import { useRef, useState } from "react";
import { Link } from "react-router-dom";
import { streamChat } from "../api/sse";

interface Msg {
  role: "user" | "assistant";
  text: string;
  citations?: string[];
}

export default function ChatPage() {
  const [messages, setMessages] = useState<Msg[]>([]);
  const [input, setInput] = useState("");
  const [streaming, setStreaming] = useState(false);
  const scrollRef = useRef<HTMLDivElement>(null);

  async function send() {
    const q = input.trim();
    if (!q || streaming) return;
    setInput("");
    const history = messages.map((m) => ({ role: m.role, text: m.text }));
    setMessages((m) => [...m, { role: "user", text: q }, { role: "assistant", text: "" }]);
    setStreaming(true);

    await streamChat(q, history, {
      onToken: (t) =>
        setMessages((m) => {
          const next = [...m];
          const last = next[next.length - 1];
          next[next.length - 1] = { ...last, text: last.text + t };
          return next;
        }),
      onDone: (d) =>
        setMessages((m) => {
          const next = [...m];
          next[next.length - 1] = { ...next[next.length - 1], citations: d.citations };
          return next;
        }),
      onError: () =>
        setMessages((m) => {
          const next = [...m];
          const last = next[next.length - 1];
          if (!last.text) next[next.length - 1] = { ...last, text: "(failed to reach the API)" };
          return next;
        }),
    });

    setStreaming(false);
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight });
  }

  return (
    <div className="mx-auto flex h-full max-w-3xl flex-col p-8">
      <h1 className="mb-4 text-xl font-semibold">Chat</h1>

      <div ref={scrollRef} className="flex-1 space-y-4 overflow-auto">
        {messages.length === 0 && (
          <p className="text-muted">Ask a question. Answers are grounded in the wiki and cited.</p>
        )}
        {messages.map((m, i) => (
          <div key={i} className={m.role === "user" ? "text-right" : ""}>
            <div
              className={`inline-block max-w-[85%] rounded-lg px-3 py-2 text-sm ${
                m.role === "user" ? "bg-accent text-accent-fg" : "card whitespace-pre-wrap"
              }`}
            >
              {m.text || (streaming ? "…" : "")}
            </div>
            {m.citations && m.citations.length > 0 && (
              <div className="mt-1 flex flex-wrap gap-2 text-xs">
                {m.citations.map((c) => (
                  <Link
                    key={c}
                    to={`/pages/${encodeURIComponent(c)}`}
                    className="text-accent hover:underline"
                  >
                    [[{c}]]
                  </Link>
                ))}
              </div>
            )}
          </div>
        ))}
      </div>

      <div className="mt-4 flex gap-2">
        <input
          className="input"
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && send()}
          placeholder="Ask the wiki…"
          disabled={streaming}
        />
        <button
          onClick={send}
          disabled={streaming || !input.trim()}
          className="rounded-lg bg-accent px-4 text-sm font-medium text-accent-fg disabled:opacity-50"
        >
          Send
        </button>
      </div>
    </div>
  );
}
