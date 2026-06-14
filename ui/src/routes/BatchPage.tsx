import { useRef, useState } from "react";
import { Link } from "react-router-dom";
import { type PreviewInput } from "../api/endpoints";
import { batchStore, useBatchItems, type BatchItem } from "../batch/store";
import {
  commitBlocked,
  effectiveRouting,
  IngestReview,
  Spinner,
} from "../components/IngestReview";
import { successMessage } from "./AddPage";

export default function BatchPage() {
  // The queue + its processing live in the module-level store, so jobs keep
  // running across navigation. Only these input fields are ephemeral page state.
  const items = useBatchItems();
  const [text, setText] = useState("");
  const [pasteName, setPasteName] = useState("");
  const [dragOver, setDragOver] = useState(false);
  const fileInput = useRef<HTMLInputElement>(null);

  function addFiles(files: FileList | null) {
    if (!files) return;
    batchStore.enqueue(
      Array.from(files).map((f) => ({
        name: f.name,
        input: { file: f, sourceRef: f.name.replace(/\.[^.]+$/, "") } as PreviewInput,
      })),
    );
  }

  function addPaste() {
    const t = text.trim();
    if (!t) return;
    const name = pasteName.trim() || t.split("\n")[0].slice(0, 40) || "pasted source";
    batchStore.enqueue([{ name, input: { text: t, sourceRef: pasteName.trim() || undefined } }]);
    setText("");
    setPasteName("");
  }

  const summary = summarize(items);
  const readyCount = items.filter(
    (it) => it.status === "ready" && it.plan && it.curation && !commitBlocked(it.plan, it.curation),
  ).length;
  const anyCommitting = items.some((it) => it.status === "committing");
  const finishedCount = items.filter((it) => it.status === "committed").length;

  return (
    <div className="mx-auto max-w-2xl p-8">
      <header className="mb-6 flex items-start justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">Add several</h1>
          <p className="mt-1 text-sm text-muted">
            Drop multiple files or add pastes. Each previews on its own — review and commit when ready.
            Jobs keep running if you browse away.
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
          <div className="mb-3 flex items-center justify-between gap-3 border-t border-border pt-4">
            <span className="text-sm text-muted">{summary}</span>
            <div className="flex items-center gap-2">
              {finishedCount > 0 && (
                <button
                  onClick={() => batchStore.clearFinished()}
                  className="rounded-lg border border-border px-3 py-2 text-sm text-muted hover:border-accent hover:text-fg"
                >
                  Clear committed
                </button>
              )}
              <button
                onClick={() => batchStore.commitAll()}
                disabled={readyCount === 0 || anyCommitting}
                className="inline-flex items-center gap-2 rounded-lg bg-accent px-4 py-2 text-sm font-medium text-accent-fg disabled:opacity-50"
              >
                {anyCommitting && <Spinner />}
                Commit all ({readyCount})
              </button>
            </div>
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
                  {item.status === "ready" && item.plan && item.curation && (
                    <button
                      onClick={() => batchStore.commit(item.id)}
                      disabled={commitBlocked(item.plan, item.curation)}
                      className="rounded border border-border px-2 py-0.5 text-xs hover:border-accent disabled:opacity-40"
                    >
                      commit
                    </button>
                  )}
                  {item.status !== "committing" && item.status !== "committed" && (
                    <button onClick={() => batchStore.remove(item.id)} className="text-muted hover:text-fg" title="remove">×</button>
                  )}
                  {item.plan && (
                    <button onClick={() => batchStore.toggleExpanded(item.id)} className="text-muted hover:text-fg" title="expand">
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
                      onChange={(patch) => batchStore.updateCuration(item.id, patch)}
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

function StatusChip({ item }: { item: BatchItem }) {
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

function summarize(items: BatchItem[]): string {
  let previewing = 0, ready = 0, conflict = 0, committed = 0, error = 0;
  for (const it of items) {
    if (it.status === "queued" || it.status === "previewing" || it.status === "committing") previewing++;
    else if (it.status === "error") error++;
    else if (it.status === "committed") committed++;
    else if (it.plan && it.curation && effectiveRouting(it.plan, it.curation).action === "contradict") conflict++;
    else ready++;
  }
  const parts: string[] = [];
  if (previewing) parts.push(`${previewing} working`);
  if (ready) parts.push(`${ready} ready`);
  if (conflict) parts.push(`${conflict} conflict`);
  if (committed) parts.push(`${committed} committed`);
  if (error) parts.push(`${error} error`);
  return parts.join(", ") || "empty";
}
