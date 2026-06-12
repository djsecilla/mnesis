import { Link } from "react-router-dom";
import { entityColor, entityTypeOf, isMutedStatus } from "../design/tokens";

/** Status chrome shared by all views: active is unadorned, stale is a muted badge. */
export function StatusBadge({ status }: { status: string }) {
  if (!isMutedStatus(status)) return null;
  return (
    <span className="ml-2 rounded border border-border px-1.5 py-0.5 text-[10px] uppercase tracking-wide text-muted">
      {status}
    </span>
  );
}

/** Page kind badge. digest is accented (synthesized), fact/note are quiet. */
export function KindBadge({ kind }: { kind: string }) {
  const accented = kind === "digest";
  return (
    <span
      className={`rounded px-1.5 py-0.5 text-[10px] uppercase tracking-wide ${
        accented ? "bg-accent text-accent-fg" : "border border-border text-muted"
      }`}
    >
      {kind}
    </span>
  );
}

/** An entity ref chip, colored by type; clicking focuses it in the graph. */
export function EntityChip({ refName }: { refName: string }) {
  const color = entityColor(entityTypeOf(refName));
  return (
    <Link
      to={`/graph?root=${encodeURIComponent(refName)}`}
      className="inline-flex items-center gap-1.5 rounded border border-border px-2 py-0.5 text-xs hover:bg-elev"
      style={{ color }}
      title={`Focus ${refName} in the graph`}
    >
      <span className="h-2 w-2 rounded-full" style={{ background: color }} />
      {refName}
    </Link>
  );
}

/** A relation triple chip: s —p→ o, with edge confidence when known. */
export function RelationChip({
  s,
  p,
  o,
  confidence,
}: {
  s: string;
  p: string;
  o: string;
  confidence?: number;
}) {
  return (
    <span className="inline-flex items-center gap-1 rounded border border-border px-2 py-0.5 text-xs text-muted">
      <span style={{ color: entityColor(entityTypeOf(s)) }}>{s}</span>
      <span className="text-muted">—{p}→</span>
      <span style={{ color: entityColor(entityTypeOf(o)) }}>{o}</span>
      {confidence !== undefined && (
        <span className="tabular-nums text-[10px] text-muted">{confidence.toFixed(2)}</span>
      )}
    </span>
  );
}
