/** Compact confidence bar + number (used in the index). */
export function ConfidenceBar({ value }: { value: number }) {
  return (
    <span className="inline-flex items-center gap-1.5">
      <span className="h-1.5 w-10 overflow-hidden rounded-full bg-border">
        <span
          className="block h-full rounded-full"
          style={{ width: `${Math.round(value * 100)}%`, background: "var(--accent)" }}
        />
      </span>
      <span className="w-8 text-right text-xs tabular-nums text-muted">{value.toFixed(2)}</span>
    </span>
  );
}

/** Confidence with a hover popover showing the Phase-2 breakdown (reader header). */
export function ConfidenceMeter({
  value,
  breakdown,
}: {
  value: number;
  breakdown: Record<string, number | boolean | string>;
}) {
  const rows: [string, unknown][] = [
    ["support", breakdown.support],
    ["retention", breakdown.retention],
    ["contradiction", breakdown.contradiction_factor],
    ["access boost", breakdown.access_boost],
  ];
  return (
    <span className="group relative inline-flex cursor-default items-center gap-1.5">
      <ConfidenceBar value={value} />
      <span className="invisible absolute left-0 top-full z-20 mt-1 w-52 rounded-lg border border-border bg-elev p-2 text-xs shadow-lg group-hover:visible">
        <span className="mb-1 block font-medium text-fg">confidence {value.toFixed(3)}</span>
        {rows.map(([k, v]) => (
          <span key={k} className="flex justify-between text-muted">
            <span>{k}</span>
            <span className="tabular-nums">
              {typeof v === "number" ? v.toFixed(3) : String(v)}
            </span>
          </span>
        ))}
      </span>
    </span>
  );
}
