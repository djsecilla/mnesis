import { useQuery } from "@tanstack/react-query";
import { useEffect } from "react";
import { Link } from "react-router-dom";
import { getEntity } from "../api/endpoints";
import type { ImpactResponse, RelatedEntity } from "../api/types";
import { KindBadge } from "../components/Badges";
import { entityColorValue, entityTypeOf } from "../design/tokens";

interface Props {
  refName: string;
  onClose: () => void;
  onExpand: (ref: string) => void;
  onFocus: (ref: string) => void; // focus another entity within the graph
  impactActive: boolean;
  onToggleImpact: () => void;
  impact?: ImpactResponse;
  impactLoading: boolean;
}

export default function GraphPanel({
  refName,
  onClose,
  onExpand,
  onFocus,
  impactActive,
  onToggleImpact,
  impact,
  impactLoading,
}: Props) {
  const { data, isLoading } = useQuery({ queryKey: ["entity", refName], queryFn: () => getEntity(refName) });
  const color = entityColorValue(entityTypeOf(refName));

  // Dismissal path 2/3: Esc closes (the x button and background-click are wired
  // by the parent). Selecting another node just swaps refName -> contents update.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const related = data?.related ?? [];
  const byPredicate = related.reduce<Record<string, RelatedEntity[]>>((acc, r) => {
    (acc[r.predicate] ??= []).push(r);
    return acc;
  }, {});

  return (
    // Floating overlay: anchored top-right with a margin, fixed width, capped
    // height with internal scroll. It does NOT participate in the graph's layout,
    // so the Cytoscape canvas keeps its size; no backdrop, graph stays interactive.
    <aside className="absolute right-3 top-3 z-20 flex max-h-[calc(100%-1.5rem)] w-80 max-w-[320px] flex-col overflow-hidden rounded-xl border border-border bg-elev shadow-lg">
      <header className="flex items-start justify-between gap-2 border-b border-border p-4">
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <span className="h-2.5 w-2.5 shrink-0 rounded-full" style={{ background: color }} />
            <h2 className="truncate text-sm font-semibold" style={{ color }} title={refName}>
              {refName}
            </h2>
          </div>
          <div className="mt-1 flex items-center gap-2 text-xs text-muted">
            <span>{data?.type ?? entityTypeOf(refName)}</span>
            {data?.confidence != null && <span className="tabular-nums">· conf {data.confidence.toFixed(2)}</span>}
          </div>
        </div>
        <button onClick={onClose} className="shrink-0 text-muted hover:text-fg" aria-label="Close">✕</button>
      </header>

      <div className="flex gap-2 border-b border-border p-3">
        <button onClick={() => onExpand(refName)} className="flex-1 rounded-lg border border-border px-3 py-1.5 text-xs hover:bg-bg">
          Expand
        </button>
        <button
          onClick={onToggleImpact}
          className={`flex-1 rounded-lg px-3 py-1.5 text-xs ${impactActive ? "bg-accent text-accent-fg" : "border border-border hover:bg-bg"}`}
        >
          {impactActive ? "Impact ✓" : "Impact"}
        </button>
      </div>

      <div className="flex-1 overflow-auto">
        {isLoading && <p className="p-4 text-sm text-muted">Loading…</p>}

        {impactActive ? (
          <section className="p-4">
            <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-muted">Impact — affected by changing this</h3>
            {impactLoading && <p className="text-sm text-muted">Computing…</p>}
            {impact && impact.affected.length === 0 && <p className="text-sm text-muted">Nothing depends on or uses it.</p>}
            <ul className="space-y-2">
              {impact?.affected.map((a) => (
                <li key={a.ref} className="text-sm">
                  <button onClick={() => onFocus(a.ref)} className="hover:underline" style={{ color: entityColorValue(entityTypeOf(a.ref)) }}>
                    {a.ref}
                  </button>
                  <span className="text-muted"> · hop {a.hop} · {a.predicate}</span>
                  <div className="mt-0.5 text-xs text-muted">{a.path.join(" → ")}</div>
                </li>
              ))}
            </ul>
          </section>
        ) : (
          data && (
            <>
              {/* Summary */}
              <section className="border-b border-border p-4">
                {data.summary ? (
                  <p className="text-sm leading-relaxed">{data.summary}</p>
                ) : (
                  <p className="text-sm italic text-muted">No summary — no active page describes this entity yet.</p>
                )}
              </section>

              {/* Tags */}
              {data.tags.length > 0 && (
                <section className="border-b border-border p-4">
                  <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-muted">Related tags</h3>
                  <div className="flex flex-wrap gap-2">
                    {data.tags.map((t) => (
                      <button
                        key={t}
                        onClick={() => onFocus(t)}
                        className="inline-flex items-center gap-1.5 rounded border border-border px-2 py-0.5 text-xs hover:bg-bg"
                        style={{ color: entityColorValue(entityTypeOf(t)) }}
                        title={`Focus ${t}`}
                      >
                        <span className="h-2 w-2 rounded-full" style={{ background: entityColorValue(entityTypeOf(t)) }} />
                        {t}
                      </button>
                    ))}
                  </div>
                </section>
              )}

              {/* Sources (provenance) */}
              {data.sources.length > 0 && (
                <section className="border-b border-border p-4">
                  <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-muted">Sources</h3>
                  <ul className="space-y-2.5">
                    {data.sources.map((s) => (
                      <li key={s.id} className="min-w-0">
                        <div className="flex items-center gap-2">
                          <Link to={`/pages/${encodeURIComponent(s.id)}`} className="min-w-0 flex-1 truncate text-sm text-accent hover:underline">
                            {s.title}
                          </Link>
                          <KindBadge kind={s.kind} />
                          <span className="shrink-0 tabular-nums text-xs text-muted">{s.confidence.toFixed(2)}</span>
                        </div>
                        {s.snippet && <p className="mt-0.5 line-clamp-2 text-xs text-muted">{s.snippet}</p>}
                      </li>
                    ))}
                  </ul>
                </section>
              )}

              {/* Related topics (typed-edge neighbours, grouped by predicate) */}
              {related.length > 0 && (
                <section className="p-4">
                  <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-muted">Related topics</h3>
                  <div className="space-y-2">
                    {Object.entries(byPredicate).map(([pred, items]) => (
                      <div key={pred}>
                        <div className="mb-1 text-[10px] uppercase tracking-wide text-muted">{pred}</div>
                        <div className="flex flex-wrap gap-2">
                          {items.map((r) => (
                            <button
                              key={`${r.direction}-${r.ref}`}
                              onClick={() => onFocus(r.ref)}
                              className="inline-flex items-center gap-1 rounded border border-border px-2 py-0.5 text-xs hover:bg-bg"
                              title={`Focus ${r.ref}`}
                            >
                              <span className="text-muted">{r.direction === "out" ? "→" : "←"}</span>
                              <span style={{ color: entityColorValue(entityTypeOf(r.ref)) }}>{r.ref}</span>
                              <span className="tabular-nums text-[10px] text-muted">{r.confidence.toFixed(2)}</span>
                            </button>
                          ))}
                        </div>
                      </div>
                    ))}
                  </div>
                </section>
              )}
            </>
          )
        )}
      </div>
    </aside>
  );
}
