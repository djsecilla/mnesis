import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { getEntity } from "../api/endpoints";
import type { ImpactResponse } from "../api/types";
import { entityColorValue, entityTypeOf } from "../design/tokens";

interface Props {
  refName: string;
  onClose: () => void;
  onExpand: (ref: string) => void;
  impactActive: boolean;
  onToggleImpact: () => void;
  impact?: ImpactResponse;
  impactLoading: boolean;
}

export default function GraphPanel({
  refName,
  onClose,
  onExpand,
  impactActive,
  onToggleImpact,
  impact,
  impactLoading,
}: Props) {
  const { data, isLoading } = useQuery({
    queryKey: ["entity", refName],
    queryFn: () => getEntity(refName),
  });
  const color = entityColorValue(entityTypeOf(refName));

  return (
    <aside className="flex w-80 shrink-0 flex-col overflow-auto border-l border-border bg-bg">
      <header className="flex items-start justify-between gap-2 border-b border-border p-4">
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <span className="h-2.5 w-2.5 shrink-0 rounded-full" style={{ background: color }} />
            <h2 className="truncate text-sm font-semibold" style={{ color }} title={refName}>
              {refName}
            </h2>
          </div>
          <p className="mt-0.5 text-xs text-muted">{data?.type ?? entityTypeOf(refName)}</p>
        </div>
        <button onClick={onClose} className="text-muted hover:text-fg" aria-label="Close">
          ✕
        </button>
      </header>

      <div className="flex gap-2 border-b border-border p-3">
        <button
          onClick={() => onExpand(refName)}
          className="flex-1 rounded-lg border border-border px-3 py-1.5 text-xs hover:bg-elev"
        >
          Expand
        </button>
        <button
          onClick={onToggleImpact}
          className={`flex-1 rounded-lg px-3 py-1.5 text-xs ${
            impactActive
              ? "bg-accent text-accent-fg"
              : "border border-border hover:bg-elev"
          }`}
        >
          {impactActive ? "Impact ✓" : "Impact"}
        </button>
      </div>

      {isLoading && <p className="p-4 text-sm text-muted">Loading…</p>}

      {impactActive ? (
        <section className="p-4">
          <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-muted">
            Impact — affected by changing this
          </h3>
          {impactLoading && <p className="text-sm text-muted">Computing…</p>}
          {impact && impact.affected.length === 0 && (
            <p className="text-sm text-muted">Nothing depends on or uses it.</p>
          )}
          <ul className="space-y-2">
            {impact?.affected.map((a) => (
              <li key={a.ref} className="text-sm">
                <span style={{ color: entityColorValue(entityTypeOf(a.ref)) }}>{a.ref}</span>
                <span className="text-muted"> · hop {a.hop} · {a.predicate}</span>
                <div className="mt-0.5 text-xs text-muted">{a.path.join(" → ")}</div>
                <div className="mt-0.5 flex flex-wrap gap-1.5">
                  {a.grounding_pages.map((p) => (
                    <Link key={p} to={`/pages/${encodeURIComponent(p)}`} className="text-accent hover:underline text-xs">
                      {p}
                    </Link>
                  ))}
                </div>
              </li>
            ))}
          </ul>
        </section>
      ) : (
        data && (
          <>
            {data.pages.length > 0 && (
              <section className="border-b border-border p-4">
                <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-muted">
                  Declared by
                </h3>
                <ul className="space-y-1">
                  {data.pages.map((p) => (
                    <li key={p}>
                      <Link to={`/pages/${encodeURIComponent(p)}`} className="text-sm text-accent hover:underline">
                        {p}
                      </Link>
                    </li>
                  ))}
                </ul>
              </section>
            )}

            <section className="p-4">
              <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-muted">
                Edges ({data.edges.length})
              </h3>
              <ul className="space-y-1 text-sm">
                {data.edges.map((e, i) => (
                  <li key={i} className={e.demoted ? "text-muted line-through" : "text-muted"}>
                    <span className="text-fg">{e.s}</span> —{e.p}→ <span className="text-fg">{e.o}</span>{" "}
                    <span className="text-xs tabular-nums">({e.confidence.toFixed(2)})</span>
                  </li>
                ))}
              </ul>
            </section>
          </>
        )
      )}
    </aside>
  );
}
