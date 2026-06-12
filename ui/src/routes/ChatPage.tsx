import { useRef, useState } from "react";
import { Link } from "react-router-dom";
import { fileback } from "../api/endpoints";
import { ChatError, streamChat, type ChatHistoryItem } from "../api/sse";
import type { RetrievalHit } from "../api/types";
import { KindBadge } from "../components/Badges";
import ChatAnswer from "../components/ChatAnswer";

interface Msg {
  role: "user" | "assistant";
  text: string;
  q?: string; // for an assistant turn: the question it answers (file-back / retry)
  done?: boolean;
  citations?: string[];
  retrieval?: RetrievalHit[];
  error?: "auth" | "stream" | null;
  groundingOpen?: boolean;
  filing?: boolean;
  filed?: { id?: string; reason?: string };
}

export default function ChatPage() {
  const [messages, setMessages] = useState<Msg[]>([]);
  const [input, setInput] = useState("");
  const [streaming, setStreaming] = useState(false);
  const scrollRef = useRef<HTMLDivElement>(null);

  const patch = (idx: number, fn: (m: Msg) => Msg) =>
    setMessages((ms) => ms.map((m, i) => (i === idx ? fn(m) : m)));

  function scrollDown() {
    requestAnimationFrame(() =>
      scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight }),
    );
  }

  async function runStream(idx: number, q: string, history: ChatHistoryItem[]) {
    patch(idx, (m) => ({
      ...m,
      text: "",
      done: false,
      error: null,
      citations: undefined,
      retrieval: undefined,
    }));
    setStreaming(true);
    await streamChat(q, history, {
      onToken: (t) => {
        patch(idx, (m) => ({ ...m, text: m.text + t }));
        scrollDown();
      },
      onDone: (d) =>
        patch(idx, (m) => ({ ...m, done: true, citations: d.citations, retrieval: d.retrieval })),
      onError: (err) =>
        patch(idx, (m) => ({
          ...m,
          done: true,
          error: err instanceof ChatError && (err.status === 401 || err.status === 403) ? "auth" : "stream",
        })),
    });
    setStreaming(false);
    scrollDown();
  }

  function send() {
    const q = input.trim();
    if (!q || streaming) return;
    setInput("");
    const base = messages;
    const history = base.map((m) => ({ role: m.role, text: m.text }));
    const idx = base.length + 1; // the assistant slot we are about to append
    setMessages([...base, { role: "user", text: q }, { role: "assistant", text: "", q, done: false }]);
    void runStream(idx, q, history);
    scrollDown();
  }

  function retry(idx: number) {
    if (streaming) return;
    const q = messages[idx].q ?? messages[idx - 1]?.text ?? "";
    const history = messages.slice(0, idx - 1).map((m) => ({ role: m.role, text: m.text }));
    void runStream(idx, q, history);
  }

  async function save(idx: number) {
    const m = messages[idx];
    if (m.filing || m.filed) return;
    patch(idx, (mm) => ({ ...mm, filing: true }));
    try {
      const res = await fileback(m.q ?? "", m.text);
      patch(idx, (mm) => ({
        ...mm,
        filing: false,
        filed: res.filed ? { id: res.digest_id ?? undefined } : { reason: res.reason },
      }));
    } catch {
      patch(idx, (mm) => ({ ...mm, filing: false, filed: { reason: "file-back failed" } }));
    }
  }

  function onKeyDown(e: React.KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      send();
    }
  }

  const streamingIdx = streaming ? messages.length - 1 : -1;

  return (
    <div className="mx-auto flex h-full max-w-3xl flex-col p-8">
      <h1 className="mb-4 text-xl font-semibold">Ask Mnesis</h1>

      <div ref={scrollRef} className="flex-1 space-y-5 overflow-auto pr-1">
        {messages.length === 0 && (
          <p className="text-muted">
            Ask a question. Answers are grounded in the wiki and cited — if the wiki has nothing,
            Mnesis says so rather than guessing.
          </p>
        )}

        {messages.map((m, i) =>
          m.role === "user" ? (
            <div key={i} className="text-right">
              <div className="inline-block max-w-[85%] whitespace-pre-wrap rounded-lg bg-accent px-3 py-2 text-left text-sm text-accent-fg">
                {m.text}
              </div>
            </div>
          ) : (
            <AssistantTurn
              key={i}
              m={m}
              streaming={i === streamingIdx}
              onToggleGrounding={() => patch(i, (mm) => ({ ...mm, groundingOpen: !mm.groundingOpen }))}
              onRetry={() => retry(i)}
              onSave={() => save(i)}
            />
          ),
        )}
      </div>

      <div className="mt-4 flex items-end gap-2">
        <textarea
          className="input min-h-[2.5rem] flex-1 resize-none py-2"
          rows={1}
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={onKeyDown}
          placeholder="Ask the wiki…  (Enter to send, Shift-Enter for newline)"
        />
        <button
          onClick={send}
          disabled={streaming || !input.trim()}
          className="rounded-lg bg-accent px-4 py-2 text-sm font-medium text-accent-fg disabled:opacity-50"
        >
          Send
        </button>
      </div>
    </div>
  );
}

function AssistantTurn({
  m,
  streaming,
  onToggleGrounding,
  onRetry,
  onSave,
}: {
  m: Msg;
  streaming: boolean;
  onToggleGrounding: () => void;
  onRetry: () => void;
  onSave: () => void;
}) {
  const grounded = !!m.done && !m.error && (m.citations?.length ?? 0) > 0;
  const notInWiki = !!m.done && !m.error && (m.citations?.length ?? 0) === 0;
  const byId = new Map((m.retrieval ?? []).map((h) => [h.id, h]));

  return (
    <div className="space-y-2">
      <div className="card px-3 py-2 text-sm">
        {m.text ? <ChatAnswer text={m.text} /> : streaming ? <Caret /> : null}
      </div>

      {/* Error states with recovery */}
      {m.error === "auth" && (
        <ErrorBar onRetry={onRetry}>
          Authentication failed — the API token was rejected. Check the configured token, then retry.
        </ErrorBar>
      )}
      {m.error === "stream" && (
        <ErrorBar onRetry={onRetry}>The answer stream was interrupted.</ErrorBar>
      )}

      {/* Honest empty state: nothing in the wiki, no confabulated answer. */}
      {notInWiki && (
        <p className="rounded-lg border border-border bg-elev px-3 py-2 text-xs text-muted">
          Mnesis has nothing on this — no wiki pages matched, so there is no grounded answer to give.
        </p>
      )}

      {grounded && (
        <>
          {/* Citation cards */}
          <div className="flex flex-wrap gap-2">
            {m.citations!.map((id, n) => {
              const h = byId.get(id);
              return (
                <Link
                  key={id}
                  to={`/pages/${encodeURIComponent(id)}`}
                  className="card flex items-center gap-2 px-2.5 py-1.5 text-xs hover:border-accent"
                >
                  <span className="inline-flex h-4 min-w-4 items-center justify-center rounded bg-accent/15 px-1 text-[10px] font-medium text-accent">
                    {n + 1}
                  </span>
                  <span className="max-w-[16rem] truncate">{h?.title ?? id}</span>
                  {h && <KindBadge kind={h.kind} />}
                  {h && (
                    <span className="tabular-nums text-muted">{h.confidence.toFixed(2)}</span>
                  )}
                </Link>
              );
            })}
          </div>

          {/* Grounding panel + file-back */}
          <div className="flex flex-wrap items-center gap-3 text-xs">
            <button onClick={onToggleGrounding} className="text-muted hover:text-fg">
              {m.groundingOpen ? "▾" : "▸"} Grounding ({m.retrieval?.length ?? 0})
            </button>
            <SaveControl m={m} onSave={onSave} />
          </div>

          {m.groundingOpen && m.retrieval && (
            <div className="overflow-hidden rounded-lg border border-border text-xs">
              <div className="grid grid-cols-[1fr_auto_auto_auto_auto] gap-x-4 bg-elev px-3 py-1.5 text-[10px] uppercase tracking-wide text-muted">
                <span>page</span>
                <span className="text-right">bm25</span>
                <span className="text-right">conf</span>
                <span className="text-right">graph</span>
                <span className="text-right">final</span>
              </div>
              {m.retrieval.map((h) => (
                <Link
                  key={h.id}
                  to={`/pages/${encodeURIComponent(h.id)}`}
                  className="grid grid-cols-[1fr_auto_auto_auto_auto] gap-x-4 px-3 py-1.5 hover:bg-elev"
                >
                  <span className="min-w-0 truncate">
                    {h.title}
                    {h.status === "stale" && <span className="ml-1 text-muted">(stale)</span>}
                  </span>
                  <span className="text-right tabular-nums text-muted">{h.bm25_score.toFixed(2)}</span>
                  <span className="text-right tabular-nums text-muted">{h.confidence.toFixed(2)}</span>
                  <span className="text-right tabular-nums text-muted">{h.graph_proximity.toFixed(2)}</span>
                  <span className="text-right tabular-nums">{h.final_score.toFixed(2)}</span>
                </Link>
              ))}
            </div>
          )}
        </>
      )}
    </div>
  );
}

function SaveControl({ m, onSave }: { m: Msg; onSave: () => void }) {
  if (m.filed?.id) {
    return (
      <span className="text-muted">
        Filed as{" "}
        <Link to={`/pages/${encodeURIComponent(m.filed.id)}`} className="text-accent hover:underline">
          {m.filed.id}
        </Link>
      </span>
    );
  }
  if (m.filed?.reason) {
    return <span className="text-muted">Not filed — {m.filed.reason}</span>;
  }
  return (
    <button
      onClick={onSave}
      disabled={m.filing}
      className="rounded border border-border px-2 py-0.5 text-muted hover:border-accent hover:text-fg disabled:opacity-50"
    >
      {m.filing ? "Saving…" : "Save to Mnesis"}
    </button>
  );
}

function ErrorBar({ children, onRetry }: { children: React.ReactNode; onRetry: () => void }) {
  return (
    <div className="flex items-center justify-between gap-3 rounded-lg border border-border bg-elev px-3 py-2 text-xs text-muted">
      <span>{children}</span>
      <button onClick={onRetry} className="rounded border border-border px-2 py-0.5 hover:border-accent hover:text-fg">
        Retry
      </button>
    </div>
  );
}

function Caret() {
  return <span className="inline-block h-4 w-1.5 animate-pulse bg-muted align-text-bottom" />;
}
