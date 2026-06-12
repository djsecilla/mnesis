import { Link } from "react-router-dom";
import type { IngestOverrides, IngestPlan, Redaction, RoutingAction } from "../api/types";
import { entityColor, entityTypeOf } from "../design/tokens";

// Shared, fully-controlled ingest review panel used by both the single Add flow
// and the batch queue. It owns no state: the parent holds the Curation and feeds
// edits back through onChange, so commit-all can read each item's overrides.

export interface Curation {
  title: string;
  tags: string[];
  tagDraft: string;
  relAccept: boolean[];
  override: { action: RoutingAction; target_page_id: string | null } | null;
  confirmStale: boolean;
}

export function initCuration(plan: IngestPlan): Curation {
  return {
    title: plan.draft_page.title,
    tags: [...plan.draft_page.tags],
    tagDraft: "",
    relAccept: plan.draft_page.relations.map(() => true),
    override: null,
    confirmStale: false,
  };
}

export function buildOverrides(c: Curation): IngestOverrides {
  const overrides: IngestOverrides = {
    title: c.title,
    tags: c.tags,
    accepted_relations: c.relAccept.map((a, i) => (a ? i : -1)).filter((i) => i >= 0),
  };
  if (c.override) overrides.routing = c.override;
  return overrides;
}

export function effectiveRouting(plan: IngestPlan, c: Curation): { action: RoutingAction; target: string | null } {
  return {
    action: c.override?.action ?? plan.routing.action,
    target: c.override?.target_page_id ?? plan.routing.target_page_id,
  };
}

/** Whether this curation is allowed to commit (supersede needs explicit confirm). */
export function commitBlocked(plan: IngestPlan, c: Curation): boolean {
  return effectiveRouting(plan, c).action === "supersede" && !c.confirmStale;
}

export function redactionSummary(reds: Redaction[]): string {
  if (!reds.length) return "No secrets or personal data detected.";
  const secret = reds.filter((r) => r.type === "secret").reduce((n, r) => n + r.count, 0);
  const parts: string[] = [];
  if (secret) parts.push(`${secret} secret${secret > 1 ? "s" : ""}`);
  for (const r of reds.filter((r) => r.type !== "secret")) {
    parts.push(`${r.count} ${r.kind}${r.count > 1 ? "s" : ""}`);
  }
  return `${parts.join(", ")} redacted before storage.`;
}

export function Spinner() {
  return <span className="h-3.5 w-3.5 animate-spin rounded-full border-2 border-current border-t-transparent" />;
}

export function IngestReview({
  plan,
  curation,
  onChange,
}: {
  plan: IngestPlan;
  curation: Curation;
  onChange: (patch: Partial<Curation>) => void;
}) {
  const { action: effAction, target: effTarget } = effectiveRouting(plan, curation);
  const needsConfirm = effAction === "supersede";

  function addTag() {
    const t = curation.tagDraft.trim();
    if (t && !curation.tags.includes(t)) onChange({ tags: [...curation.tags, t], tagDraft: "" });
    else onChange({ tagDraft: "" });
  }

  return (
    <div className="space-y-5">
      {/* redaction summary + warnings */}
      <p className="text-sm text-muted">🛡 {redactionSummary(plan.redactions)}</p>
      {plan.warnings.length > 0 && (
        <div className="rounded-lg border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-sm">
          {plan.warnings.map((w, i) => (
            <div key={i}>⚠ {w}</div>
          ))}
        </div>
      )}

      {/* extracted page */}
      <div className="space-y-2">
        <label className="text-xs uppercase tracking-wide text-muted">Title</label>
        <input
          value={curation.title}
          onChange={(e) => onChange({ title: e.target.value })}
          className="input w-full py-2 text-base"
        />
        {plan.draft_page.summary_markdown && (
          <p className="text-sm leading-relaxed text-muted">{plan.draft_page.summary_markdown}</p>
        )}
      </div>

      {/* tags */}
      <div className="space-y-2">
        <label className="text-xs uppercase tracking-wide text-muted">Tags</label>
        <div className="flex flex-wrap items-center gap-2">
          {curation.tags.map((t) => (
            <span
              key={t}
              className="inline-flex items-center gap-1.5 rounded border border-border px-2 py-0.5 text-xs"
              style={{ color: entityColor(entityTypeOf(t)) }}
            >
              <span className="h-2 w-2 rounded-full" style={{ background: entityColor(entityTypeOf(t)) }} />
              {t}
              <button onClick={() => onChange({ tags: curation.tags.filter((x) => x !== t) })} className="text-muted hover:text-fg">×</button>
            </span>
          ))}
          <input
            value={curation.tagDraft}
            onChange={(e) => onChange({ tagDraft: e.target.value })}
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
              <label key={i} className={`flex cursor-pointer items-center gap-2 text-xs ${curation.relAccept[i] ? "" : "opacity-40"}`}>
                <input
                  type="checkbox"
                  checked={curation.relAccept[i] ?? true}
                  onChange={() => onChange({ relAccept: curation.relAccept.map((a, j) => (j === i ? !a : a)) })}
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
          <OverrideBtn label="Create new page" active={effAction === "new"} onClick={() => onChange({ override: { action: "new", target_page_id: null } })} />
          <OverrideBtn label="Use suggested" active={curation.override === null} onClick={() => onChange({ override: null })} />
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
                <OverrideBtn label="Reinforce" active={effAction === "reinforce" && effTarget === c.page_id} onClick={() => onChange({ override: { action: "reinforce", target_page_id: c.page_id } })} />
                <OverrideBtn label="Supersede" active={effAction === "supersede" && effTarget === c.page_id} onClick={() => onChange({ override: { action: "supersede", target_page_id: c.page_id } })} />
              </div>
            ))}
          </div>
        )}

        {needsConfirm && effTarget && (
          <label className="flex items-center gap-2 rounded-lg border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-sm">
            <input type="checkbox" checked={curation.confirmStale} onChange={(e) => onChange({ confirmStale: e.target.checked })} />
            <span>This marks <span className="font-medium">{effTarget}</span> stale.</span>
          </label>
        )}
      </div>
    </div>
  );
}

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
