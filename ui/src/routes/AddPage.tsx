import { useMutation } from "@tanstack/react-query";
import { useRef, useState } from "react";
import { Link } from "react-router-dom";
import { ingestCommit, ingestPreview } from "../api/endpoints";
import type { IngestOverrides, IngestPlan, IngestResult, Redaction, RoutingAction } from "../api/types";
import { entityColor, entityTypeOf } from "../design/tokens";

export default function AddPage() {
  // --- input ---
  const [text, setText] = useState("");
  const [sourceName, setSourceName] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const [dragOver, setDragOver] = useState(false);
  const fileInput = useRef<HTMLInputElement>(null);

  // --- curation (initialized from a preview) ---
  const [plan, setPlan] = useState<IngestPlan | null>(null);
  const [title, setTitle] = useState("");
  const [tags, setTags] = useState<string[]>([]);
  const [tagDraft, setTagDraft] = useState("");
  const [relAccept, setRelAccept] = useState<boolean[]>([]);
  const [override, setOverride] = useState<{ action: RoutingAction; target_page_id: string | null } | null>(null);
  const [confirmStale, setConfirmStale] = useState(false);
  const [result, setResult] = useState<IngestResult | null>(null);

  const preview = useMutation({
    mutationFn: () =>
      ingestPreview(file ? { file, sourceRef: sourceName || undefined } : { text, sourceRef: sourceName || undefined }),
    onSuccess: (p) => {
      setPlan(p);
      setTitle(p.draft_page.title);
      setTags(p.draft_page.tags);
      setRelAccept(p.draft_page.relations.map(() => true));
      setOverride(null);
      setConfirmStale(false);
      setResult(null);
    },
  });

  const commit = useMutation({
    mutationFn: () => {
      const overrides: IngestOverrides = {
        title,
        tags,
        accepted_relations: relAccept.map((a, i) => (a ? i : -1)).filter((i) => i >= 0),
      };
      if (override) overrides.routing = override;
      return ingestCommit(plan!, overrides);
    },
    onSuccess: (r) => setResult(r),
  });

  function reset() {
    setText("");
    setSourceName("");
    setFile(null);
    setPlan(null);
    setResult(null);
    setOverride(null);
    setConfirmStale(false);
    preview.reset();
    commit.reset();
  }

  function takeFile(f: File | null) {
    setFile(f);
    setPlan(null);
    if (f && !sourceName) setSourceName(f.name.replace(/\.[^.]+$/, ""));
  }

  const canPreview = (!!file || text.trim().length > 0) && !preview.isPending;

  return (
    <div className="mx-auto max-w-2xl p-8">
      <header className="mb-6">
        <h1 className="text-2xl font-semibold tracking-tight">Add to Mnesis</h1>
        <p className="mt-1 text-sm text-muted">
          Paste or upload a source. Mnesis redacts secrets, extracts a page, and shows you exactly
          what it will do — nothing is written until you confirm.
        </p>
      </header>

      {result ? (
        <Success result={result} tags={tags} onAddAnother={reset} />
      ) : (
        <div className="space-y-6">
          {/* --- input --- */}
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
                onDrop={(e) => {
                  e.preventDefault();
                  setDragOver(false);
                  takeFile(e.dataTransfer.files?.[0] ?? null);
                }}
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
            {preview.isError && (
              <p className="text-sm text-red-400">{(preview.error as Error).message}</p>
            )}
          </section>

          {/* --- review panel --- */}
          {plan && (
            <ReviewPanel
              plan={plan}
              title={title}
              setTitle={setTitle}
              tags={tags}
              setTags={setTags}
              tagDraft={tagDraft}
              setTagDraft={setTagDraft}
              relAccept={relAccept}
              setRelAccept={setRelAccept}
              override={override}
              setOverride={setOverride}
              confirmStale={confirmStale}
              setConfirmStale={setConfirmStale}
              commit={commit}
            />
          )}
        </div>
      )}
    </div>
  );
}

// --- review panel -----------------------------------------------------------

function ReviewPanel(props: {
  plan: IngestPlan;
  title: string;
  setTitle: (s: string) => void;
  tags: string[];
  setTags: (t: string[]) => void;
  tagDraft: string;
  setTagDraft: (s: string) => void;
  relAccept: boolean[];
  setRelAccept: (b: boolean[]) => void;
  override: { action: RoutingAction; target_page_id: string | null } | null;
  setOverride: (o: { action: RoutingAction; target_page_id: string | null } | null) => void;
  confirmStale: boolean;
  setConfirmStale: (b: boolean) => void;
  commit: ReturnType<typeof useMutation<IngestResult, Error, void>>;
}) {
  const { plan, title, setTitle, tags, setTags, tagDraft, setTagDraft, relAccept, setRelAccept } = props;
  const { override, setOverride, confirmStale, setConfirmStale, commit } = props;

  const effAction: RoutingAction = override?.action ?? plan.routing.action;
  const effTarget = override?.target_page_id ?? plan.routing.target_page_id;
  const needsConfirm = effAction === "supersede";
  const commitDisabled = commit.isPending || (needsConfirm && !confirmStale);

  function addTag() {
    const t = tagDraft.trim();
    if (t && !tags.includes(t)) setTags([...tags, t]);
    setTagDraft("");
  }

  return (
    <section className="space-y-5 border-t border-border pt-5">
      {/* redaction summary + warnings */}
      <p className="text-sm text-muted">🛡 {redactionSummary(plan.redactions)}</p>
      {plan.warnings.length > 0 && (
        <div className="rounded-lg border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-sm">
          {plan.warnings.map((w, i) => <div key={i}>⚠ {w}</div>)}
        </div>
      )}

      {/* extracted page */}
      <div className="space-y-2">
        <label className="text-xs uppercase tracking-wide text-muted">Title</label>
        <input value={title} onChange={(e) => setTitle(e.target.value)} className="input w-full py-2 text-base" />
        {plan.draft_page.summary_markdown && (
          <p className="text-sm leading-relaxed text-muted">{plan.draft_page.summary_markdown}</p>
        )}
      </div>

      {/* tags */}
      <div className="space-y-2">
        <label className="text-xs uppercase tracking-wide text-muted">Tags</label>
        <div className="flex flex-wrap items-center gap-2">
          {tags.map((t) => (
            <span
              key={t}
              className="inline-flex items-center gap-1.5 rounded border border-border px-2 py-0.5 text-xs"
              style={{ color: entityColor(entityTypeOf(t)) }}
            >
              <span className="h-2 w-2 rounded-full" style={{ background: entityColor(entityTypeOf(t)) }} />
              {t}
              <button onClick={() => setTags(tags.filter((x) => x !== t))} className="text-muted hover:text-fg">×</button>
            </span>
          ))}
          <input
            value={tagDraft}
            onChange={(e) => setTagDraft(e.target.value)}
            onKeyDown={(e) => { if (e.key === "Enter") { e.preventDefault(); addTag(); } }}
            placeholder="add tag…"
            className="w-28 bg-transparent text-xs text-fg placeholder:text-muted focus:outline-none"
          />
        </div>
      </div>

      {/* relations */}
      {plan.draft_page.relations.length > 0 && (
        <div className="space-y-2">
          <label className="text-xs uppercase tracking-wide text-muted">Relations</label>
          <div className="space-y-1.5">
            {plan.draft_page.relations.map((r, i) => (
              <label
                key={i}
                className={`flex cursor-pointer items-center gap-2 text-xs ${relAccept[i] ? "" : "opacity-40"}`}
              >
                <input
                  type="checkbox"
                  checked={relAccept[i] ?? true}
                  onChange={() => setRelAccept(relAccept.map((a, j) => (j === i ? !a : a)))}
                  className="accent-[var(--accent)]"
                />
                <span style={{ color: entityColor(entityTypeOf(r.s)) }}>{r.s}</span>
                <span className="text-muted">—{r.p}→</span>
                <span style={{ color: entityColor(entityTypeOf(r.o)) }}>{r.o}</span>
              </label>
            ))}
          </div>
        </div>
      )}

      {/* routing */}
      <div className="space-y-2">
        <label className="text-xs uppercase tracking-wide text-muted">What will happen</label>
        <div className="rounded-lg border border-border bg-elev px-3 py-2 text-sm">
          <RouteStatement action={effAction} targetId={effTarget} />
        </div>

        <div className="flex flex-wrap gap-2 pt-1 text-xs">
          <OverrideBtn
            label="Create new page"
            active={effAction === "new"}
            onClick={() => setOverride({ action: "new", target_page_id: null })}
          />
          <OverrideBtn
            label="Use suggested"
            active={override === null}
            onClick={() => setOverride(null)}
          />
        </div>

        {plan.routing.candidates.length > 0 && (
          <div className="space-y-1.5 pt-1">
            <span className="text-xs text-muted">Candidate matches:</span>
            {plan.routing.candidates.map((c) => (
              <div key={c.page_id} className="flex items-center gap-2 text-xs">
                <Link to={`/pages/${encodeURIComponent(c.page_id)}`} className="min-w-0 flex-1 truncate text-accent hover:underline">
                  {c.title}
                </Link>
                <span className="tabular-nums text-muted">{c.confidence.toFixed(2)}</span>
                <OverrideBtn
                  label="Reinforce"
                  active={effAction === "reinforce" && effTarget === c.page_id}
                  onClick={() => setOverride({ action: "reinforce", target_page_id: c.page_id })}
                />
                <OverrideBtn
                  label="Supersede"
                  active={effAction === "supersede" && effTarget === c.page_id}
                  onClick={() => setOverride({ action: "supersede", target_page_id: c.page_id })}
                />
              </div>
            ))}
          </div>
        )}

        {needsConfirm && effTarget && (
          <label className="flex items-center gap-2 rounded-lg border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-sm">
            <input type="checkbox" checked={confirmStale} onChange={(e) => setConfirmStale(e.target.checked)} />
            <span>This marks <span className="font-medium">{effTarget}</span> stale.</span>
          </label>
        )}
      </div>

      {/* commit */}
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
  );
}

// --- success ----------------------------------------------------------------

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
        <Link
          to={entityRoot ? `/graph?root=${encodeURIComponent(entityRoot)}` : "/graph"}
          className="rounded-lg border border-border px-4 py-2 hover:border-accent"
        >
          View in graph
        </Link>
        <button onClick={onAddAnother} className="rounded-lg border border-border px-4 py-2 hover:border-accent">
          Add another
        </button>
      </div>
    </div>
  );
}

// --- small pieces -----------------------------------------------------------

function RouteStatement({ action, targetId }: { action: RoutingAction; targetId: string | null }) {
  const link = targetId ? (
    <Link to={`/pages/${encodeURIComponent(targetId)}`} className="text-accent hover:underline">{targetId}</Link>
  ) : null;
  if (action === "new") return <span>Will <span className="font-medium">create a new page</span>.</span>;
  if (action === "reinforce") return <span>Will <span className="font-medium">reinforce</span> {link}.</span>;
  if (action === "supersede")
    return <span>Will <span className="font-medium">supersede</span> {link} <span className="text-muted">(marks it stale)</span>.</span>;
  return <span><span className="font-medium">Conflicts with</span> {link} — both kept, a review is queued.</span>;
}

function OverrideBtn({ label, active, onClick }: { label: string; active: boolean; onClick: () => void }) {
  return (
    <button
      onClick={onClick}
      className={`rounded border px-2 py-0.5 ${active ? "border-accent bg-accent/10 text-fg" : "border-border text-muted hover:text-fg"}`}
    >
      {label}
    </button>
  );
}

function Spinner() {
  return <span className="h-3.5 w-3.5 animate-spin rounded-full border-2 border-current border-t-transparent" />;
}

function redactionSummary(reds: Redaction[]): string {
  if (!reds.length) return "No secrets or personal data detected.";
  const secret = reds.filter((r) => r.type === "secret").reduce((n, r) => n + r.count, 0);
  const parts: string[] = [];
  if (secret) parts.push(`${secret} secret${secret > 1 ? "s" : ""}`);
  for (const r of reds.filter((r) => r.type !== "secret")) {
    parts.push(`${r.count} ${r.kind}${r.count > 1 ? "s" : ""}`);
  }
  return `${parts.join(", ")} redacted before storage.`;
}

function successMessage(r: IngestResult): string {
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
