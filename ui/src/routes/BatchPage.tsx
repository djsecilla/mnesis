import { useQueryClient } from "@tanstack/react-query";
import { useRef, useState } from "react";
import { Link } from "react-router-dom";
import { ingestCommit, ingestPreview, type PreviewInput } from "../api/endpoints";
import type { IngestPlan, IngestResult } from "../api/types";
import {
  buildOverrides,
  commitBlocked,
  effectiveRouting,
  IngestReview,
  initCuration,
  Spinner,
} from "../components/IngestReview";
import type { Curation } from "../components/IngestReview";
import { successMessage } from "./AddPage";

type Status = "queued" | "previewing" | "ready" | "committing" | "committed" | "error";

interface Item {
  id: number;
  name: string;
  input: PreviewInput;
  status: Status;
  plan?: IngestPlan;
  curation?: Curation;
  result?: IngestResult;
  error?: string;
  expanded: boolean;
}

const PREVIEW_CONCURRENCY = 3;

async function runPool(tasks: Array<() => Promise<void>>, limit: number): Promise<void> {
  let i = 0;
  await Promise.all(
    Array.from({ length: Math.min(limit, tasks.length) }, async () => {
      while (i < tasks.length) await tasks[i++]();
    }),
  );
}

export default function BatchPage() {
  const [items, setItems] = useState<Item[]>([]);
  const [text, setText] = useState("");
  const [pasteName, setPasteName] = useState("");
  const [dragOver, setDragOver] = useState(false);
  const [committingAll, setCommittingAll] = useState(false);
  const fileInput = useRef<HTMLInputElement>(null);
  const nextId = useRef(1);
  const itemsRef = useRef(items);
  itemsRef.current = items;
  const qc = useQueryClient();

  function update(id: number, patch: Partial<Item>) {
    setItems((prev) => prev.map((it) => (it.id === id ? { ...it, ...patch } : it)));
  }

  async function previewItem(id: number, input: PreviewInput) {
    update(id, { status: "previewing", error: undefined });
    try {
      const plan = await ingestPreview(input);
      update(id, { status: "ready", plan, curation: initCuration(plan) });
    } catch (e) {
      update(id, { status: "error", error: (e as Error).message });
    }
  }

  function enqueue(entries: Array<{ name: string; input: PreviewInput }>) {
    if (entries.length === 0) return;
    const startEmpty = itemsRef.current.length === 0;
    const newItems: Item[] = entries.map((e, idx) => ({
      id: nextId.current++,
      name: e.name,
      input: e.input,
      status: "queued",
      expanded: startEmpty && idx === 0, // auto-expand the first when starting fresh
    }));
    setItems((prev) => [...prev, ...newItems]);
    void runPool(newItems.map((it) => () => previewItem(it.id, it.input)), PREVIEW_CONCURRENCY);
  }

  function addFiles(files: FileList | null) {
    if (!files) return;
    enqueue(Array.from(files).map((f) => ({ name: f.name, input: { file: f, sourceRef: f.name.replace(/\.[^.]+$/, "") } })));
  }

  function addPaste() {
    const t = text.trim();
    if (!t) return;
    const name = pasteName.trim() || t.split("\n")[0].slice(0, 40) || "pasted source";
    enqueue([{ name, input: { text: t, sourceRef: pasteName.trim() || undefined } }]);
    setText("");
    setPasteName("");
  }

  async function commitItem(item: Item) {
    if (!item.plan || !item.curation) return;
    update(item.id, { status: "committing", error: undefined });
    try {
      const result = await ingestCommit(item.plan, buildOverrides(item.curation));
      update(item.id, { status: "committed", result });
      for (const key of [["pages"], ["graph"], ["palette-graph"], ["sources"], ["reviews"]]) {
        qc.invalidateQueries({ queryKey: key });
      }
    } catch (e) {
      update(item.id, { status: "error", error: (e as Error).message });
    }
  }

  async function commitAll() {
    const ready = itemsRef.current.filter((it) => it.status === "ready" && it.plan && it.curation && !commitBlocked(it.plan, it.curation));
    setCommittingAll(true);
    await runPool(ready.map((it) => () => commitItem(it)), PREVIEW_CONCURRENCY);
    setCommittingAll(false);
  }

  const summary = summarize(items);
  const readyCount = items.filter((it) => it.status === "ready" && it.plan && it.curation && !commitBlocked(it.plan, it.curation)).length;

  return (
    <div className="mx-auto max-w-2xl p-8">
      <header className="mb-6 flex items-start justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">Add several</h1>
          <p className="mt-1 text-sm text-muted">
            Drop multiple files or add pastes. Each previews on its own — review and commit when ready.
          </p>
        </div>
        <Link to="/add" className="shrink-0 whitespace-nowrap text-sm text-accent hover:underline">← Single source</Link>
      </header>

      {/* input */}
      <section className="space-y-3">
        <div
          onDragOver={(e) => { e.preventDefault(); setDragOver(true); }}
          onDragLeave={() => setDragOver(false)}
          onDrop={(e) => { e.preventDefault(); setDragOver(false); addFiles(e.dataTransfer.files); }}
          onClick={() => fileInput.current?.click()}
          className={`cursor-pointer rounded-lg border border-dashed px-3 py-5 text-center text-sm text-muted transition ${
            dragOver ? "border-accent bg-accent/5 text-fg" : "border-border hover:border-accent"
          }`}
        >
          Drop <span className="text-fg">multiple</span> .md / text files here, or click to browse
          <input
            ref={fileInput}
            type="file"
            multiple
            accept=".md,.markdown,.txt,text/markdown,text/plain"
            className="hidden"
            onChange={(e) => addFiles(e.target.files)}
          />
        </div>
        <div className="flex items-start gap-2">
          <textarea
            value={text}
            onChange={(e) => setText(e.target.value)}
            placeholder="…or paste a source and add it to the queue"
            className="input min-h-[3rem] flex-1 resize-y py-2 text-sm"
          />
          <div className="flex w-40 flex-col gap-2">
            <input value={pasteName} onChange={(e) => setPasteName(e.target.value)} placeholder="name (optional)" className="input py-1.5 text-sm" />
            <button onClick={addPaste} disabled={!text.trim()} className="rounded-lg border border-border px-3 py-1.5 text-sm hover:border-accent disabled:opacity-50">
              Add to queue
            </button>
          </div>
        </div>
      </section>

      {/* queue */}
      {items.length > 0 && (
        <section className="mt-6">
          <div className="mb-3 flex items-center justify-between border-t border-border pt-4">
            <span className="text-sm text-muted">{summary}</span>
            <button
              onClick={commitAll}
              disabled={readyCount === 0 || committingAll}
              className="inline-flex items-center gap-2 rounded-lg bg-accent px-4 py-2 text-sm font-medium text-accent-fg disabled:opacity-50"
            >
              {committingAll && <Spinner />}
              Commit all ({readyCount})
            </button>
          </div>

          <ul className="space-y-2">
            {items.map((item) => (
              <li key={item.id} className="card overflow-hidden">
                <div className="flex items-center gap-3 px-3 py-2">
                  <StatusChip item={item} />
                  <span className="min-w-0 flex-1 truncate text-sm">{item.name}</span>
                  {item.status === "committed" && item.result && (
                    <Link to={`/pages/${encodeURIComponent(item.result.page_id)}`} className="text-xs text-accent hover:underline">
                      open page →
                    </Link>
                  )}
                  {(item.status === "ready") && item.plan && item.curation && (
                    <button
                      onClick={() => commitItem(item)}
                      disabled={commitBlocked(item.plan, item.curation)}
                      className="rounded border border-border px-2 py-0.5 text-xs hover:border-accent disabled:opacity-40"
                    >
                      commit
                    </button>
                  )}
                  {item.status !== "committing" && item.status !== "committed" && (
                    <button onClick={() => setItems((p) => p.filter((x) => x.id !== item.id))} className="text-muted hover:text-fg" title="remove">×</button>
                  )}
                  {item.plan && (
                    <button onClick={() => update(item.id, { expanded: !item.expanded })} className="text-muted hover:text-fg" title="expand">
                      {item.expanded ? "▾" : "▸"}
                    </button>
                  )}
                </div>

                {item.status === "error" && (
                  <p className="border-t border-border px-3 py-2 text-xs text-red-400">{item.error}</p>
                )}

                {item.expanded && item.plan && item.curation && item.status !== "committed" && (
                  <div className="border-t border-border px-3 py-3">
                    <IngestReview
                      plan={item.plan}
                      curation={item.curation}
                      onChange={(patch) => update(item.id, { curation: { ...item.curation!, ...patch } })}
                    />
                  </div>
                )}

                {item.status === "committed" && item.result && (
                  <p className="border-t border-border px-3 py-2 text-xs text-muted">✓ {successMessage(item.result)}</p>
                )}
              </li>
            ))}
          </ul>
        </section>
      )}
    </div>
  );
}

function StatusChip({ item }: { item: Item }) {
  if (item.status === "queued" || item.status === "previewing")
    return <span className="inline-flex items-center gap-1.5 text-xs text-muted"><Spinner /> previewing</span>;
  if (item.status === "committing")
    return <span className="inline-flex items-center gap-1.5 text-xs text-muted"><Spinner /> committing</span>;
  if (item.status === "committed")
    return <span className="rounded bg-accent px-1.5 py-0.5 text-[10px] uppercase tracking-wide text-accent-fg">committed</span>;
  if (item.status === "error")
    return <span className="rounded border border-red-500/50 px-1.5 py-0.5 text-[10px] uppercase tracking-wide text-red-400">error</span>;

  // ready: show routing flavor
  const action = item.plan && item.curation ? effectiveRouting(item.plan, item.curation).action : "new";
  const conflict = action === "contradict";
  return (
    <span
      className={`rounded border px-1.5 py-0.5 text-[10px] uppercase tracking-wide ${
        conflict ? "border-amber-500/50 text-amber-400" : "border-border text-muted"
      }`}
    >
      {conflict ? "conflict" : action}
    </span>
  );
}

function summarize(items: Item[]): string {
  let previewing = 0, ready = 0, conflict = 0, committed = 0, error = 0;
  for (const it of items) {
    if (it.status === "queued" || it.status === "previewing" || it.status === "committing") previewing++;
    else if (it.status === "error") error++;
    else if (it.status === "committed") committed++;
    else if (it.plan && it.curation && effectiveRouting(it.plan, it.curation).action === "contradict") conflict++;
    else ready++;
  }
  const parts: string[] = [];
  if (previewing) parts.push(`${previewing} previewing`);
  if (ready) parts.push(`${ready} ready`);
  if (conflict) parts.push(`${conflict} conflict`);
  if (committed) parts.push(`${committed} committed`);
  if (error) parts.push(`${error} error`);
  return parts.join(", ") || "empty";
}
