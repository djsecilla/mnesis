import { useMutation } from "@tanstack/react-query";
import { useRef, useState } from "react";
import { Link } from "react-router-dom";
import { ingestCommit, ingestPreview } from "../api/endpoints";
import type { IngestPlan, IngestResult } from "../api/types";
import { buildOverrides, commitBlocked, IngestReview, initCuration, Spinner } from "../components/IngestReview";
import type { Curation } from "../components/IngestReview";

export default function AddPage() {
  const [text, setText] = useState("");
  const [sourceName, setSourceName] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const [dragOver, setDragOver] = useState(false);
  const fileInput = useRef<HTMLInputElement>(null);

  const [plan, setPlan] = useState<IngestPlan | null>(null);
  const [curation, setCuration] = useState<Curation | null>(null);
  const [result, setResult] = useState<IngestResult | null>(null);

  const preview = useMutation({
    mutationFn: () =>
      ingestPreview(file ? { file, sourceRef: sourceName || undefined } : { text, sourceRef: sourceName || undefined }),
    onSuccess: (p) => {
      setPlan(p);
      setCuration(initCuration(p));
      setResult(null);
    },
  });

  const commit = useMutation({
    mutationFn: () => ingestCommit(plan!, buildOverrides(curation!)),
    onSuccess: (r) => setResult(r),
  });

  function reset() {
    setText("");
    setSourceName("");
    setFile(null);
    setPlan(null);
    setCuration(null);
    setResult(null);
    preview.reset();
    commit.reset();
  }

  function takeFile(f: File | null) {
    setFile(f);
    setPlan(null);
    if (f && !sourceName) setSourceName(f.name.replace(/\.[^.]+$/, ""));
  }

  const canPreview = (!!file || text.trim().length > 0) && !preview.isPending;
  const commitDisabled = !plan || !curation || commit.isPending || commitBlocked(plan, curation);

  return (
    <div className="mx-auto max-w-2xl p-8">
      <header className="mb-6 flex items-start justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">Add to Mnesis</h1>
          <p className="mt-1 text-sm text-muted">
            Paste or upload a source. Mnesis redacts secrets, extracts a page, and shows you exactly
            what it will do — nothing is written until you confirm.
          </p>
        </div>
        <Link to="/add/batch" className="shrink-0 whitespace-nowrap text-sm text-accent hover:underline">
          Add several →
        </Link>
      </header>

      {result ? (
        <Success result={result} tags={curation?.tags ?? []} onAddAnother={reset} />
      ) : (
        <div className="space-y-6">
          <section className="space-y-3">
            {file ? (
              <div className="flex items-center justify-between rounded-lg border border-border bg-elev px-3 py-2 text-sm">
                <span>📎 {file.name} <span className="text-muted">({file.size} bytes)</span></span>
                <button onClick={() => takeFile(null)} className="text-muted hover:text-fg">remove</button>
              </div>
            ) : (
              <textarea
                value={text}
                onChange={(e) => { setText(e.target.value); setPlan(null); }}
                placeholder="Paste a source — notes, a doc, a decision, a snippet…"
                className="input min-h-[8rem] w-full resize-y py-2 leading-relaxed"
              />
            )}

            {!file && (
              <div
                onDragOver={(e) => { e.preventDefault(); setDragOver(true); }}
                onDragLeave={() => setDragOver(false)}
                onDrop={(e) => { e.preventDefault(); setDragOver(false); takeFile(e.dataTransfer.files?.[0] ?? null); }}
                onClick={() => fileInput.current?.click()}
                className={`cursor-pointer rounded-lg border border-dashed px-3 py-4 text-center text-sm text-muted transition ${
                  dragOver ? "border-accent bg-accent/5 text-fg" : "border-border hover:border-accent"
                }`}
              >
                Drop a <span className="text-fg">.md</span> / text file here, or click to browse
                <input
                  ref={fileInput}
                  type="file"
                  accept=".md,.markdown,.txt,text/markdown,text/plain"
                  className="hidden"
                  onChange={(e) => takeFile(e.target.files?.[0] ?? null)}
                />
              </div>
            )}

            <div className="flex items-center gap-2">
              <input
                value={sourceName}
                onChange={(e) => setSourceName(e.target.value)}
                placeholder="source name (optional)"
                className="input flex-1 py-1.5 text-sm"
              />
              <button
                onClick={() => preview.mutate()}
                disabled={!canPreview}
                className="inline-flex items-center gap-2 rounded-lg bg-accent px-4 py-2 text-sm font-medium text-accent-fg disabled:opacity-50"
              >
                {preview.isPending && <Spinner />}
                {preview.isPending ? "Analyzing…" : plan ? "Re-preview" : "Preview"}
              </button>
            </div>
            {preview.isError && <p className="text-sm text-red-400">{(preview.error as Error).message}</p>}
          </section>

          {plan && curation && (
            <section className="space-y-5 border-t border-border pt-5">
              <IngestReview plan={plan} curation={curation} onChange={(p) => setCuration((c) => ({ ...c!, ...p }))} />
              <div className="flex items-center gap-3 border-t border-border pt-4">
                <button
                  onClick={() => commit.mutate()}
                  disabled={commitDisabled}
                  className="inline-flex items-center gap-2 rounded-lg bg-accent px-4 py-2 text-sm font-medium text-accent-fg disabled:opacity-50"
                >
                  {commit.isPending && <Spinner />}
                  {commit.isPending ? "Adding…" : "Add to Mnesis"}
                </button>
                <span className="text-xs text-muted">Writes a page and commits to the wiki.</span>
              </div>
              {commit.isError && <p className="text-sm text-red-400">{(commit.error as Error).message}</p>}
            </section>
          )}
        </div>
      )}
    </div>
  );
}

function Success({ result, tags, onAddAnother }: { result: IngestResult; tags: string[]; onAddAnother: () => void }) {
  const entityRoot = tags.find((t) => /^[a-z][a-z-]*:/.test(t));
  return (
    <div className="space-y-4 rounded-lg border border-border bg-elev p-6">
      <div className="text-lg font-medium">✓ {successMessage(result)}</div>
      {result.redaction_count > 0 && (
        <p className="text-sm text-muted">{result.redaction_count} value(s) were redacted before storage.</p>
      )}
      <div className="flex flex-wrap gap-3 text-sm">
        <Link to={`/pages/${encodeURIComponent(result.page_id)}`} className="rounded-lg bg-accent px-4 py-2 font-medium text-accent-fg">
          Open page
        </Link>
        <Link to={entityRoot ? `/graph?root=${encodeURIComponent(entityRoot)}` : "/graph"} className="rounded-lg border border-border px-4 py-2 hover:border-accent">
          View in graph
        </Link>
        <button onClick={onAddAnother} className="rounded-lg border border-border px-4 py-2 hover:border-accent">
          Add another
        </button>
      </div>
    </div>
  );
}

export function successMessage(r: IngestResult): string {
  switch (r.action_taken) {
    case "new":
      return "Created a new page.";
    case "reinforce":
      return "Reinforced an existing page.";
    case "supersede":
      return r.superseded_id ? `Superseded ${r.superseded_id} and created a new page.` : "Superseded and created a new page.";
    case "contradict":
      return "Recorded alongside the conflicting page; a review was queued.";
    default:
      return "Done.";
  }
}
