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

/** An entity ref shown with its type color (a dot + the ref). */
export function EntityChip({ refName }: { refName: string }) {
  const color = entityColor(entityTypeOf(refName));
  return (
    <span
      className="inline-flex items-center gap-1.5 rounded border border-border px-2 py-0.5 text-xs"
      style={{ color }}
      title={refName}
    >
      <span className="h-2 w-2 rounded-full" style={{ background: color }} />
      {refName}
    </span>
  );
}
