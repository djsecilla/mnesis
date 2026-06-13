import { BRAND_MARK, type BrandMark } from "../design/tokens";

// Presentational, dependency-free brand mark. Ink uses `currentColor`, so the
// mark inherits the surrounding text color and inverts with the theme for free.
// The accent (memory) node reads the --accent token via an inline style — CSS
// var() does NOT resolve inside an SVG fill *attribute*, but it does in a style.
const ACCENT: React.CSSProperties = { fill: "var(--accent)" };

function Mark({ variant }: { variant: BrandMark }) {
  switch (variant) {
    case "compounding":
      return (
        <>
          <circle cx="32" cy="32" r="24" fill="none" stroke="currentColor" strokeWidth="2.4" opacity="0.30" />
          <circle cx="32" cy="32" r="17" fill="none" stroke="currentColor" strokeWidth="2.9" opacity="0.55" />
          <circle cx="32" cy="32" r="10" fill="none" stroke="currentColor" strokeWidth="3.4" />
          <circle cx="32" cy="22" r="2.6" style={ACCENT} />
          <circle cx="49" cy="32" r="2.2" fill="currentColor" />
          <circle cx="15" cy="49" r="2" fill="currentColor" />
          <circle cx="32" cy="32" r="5" style={ACCENT} />
        </>
      );
    case "constellation":
      return (
        <>
          <g stroke="currentColor" strokeWidth="1.7" opacity="0.4">
            <line x1="33" y1="32" x2="20" y2="21" />
            <line x1="33" y1="32" x2="46" y2="18" />
            <line x1="33" y1="32" x2="49" y2="42" />
            <line x1="33" y1="32" x2="23" y2="46" />
            <line x1="20" y1="21" x2="23" y2="46" />
            <line x1="46" y1="18" x2="49" y2="42" />
          </g>
          <g fill="currentColor">
            <circle cx="20" cy="21" r="3" />
            <circle cx="46" cy="18" r="2.6" />
            <circle cx="49" cy="42" r="3" />
            <circle cx="23" cy="46" r="2.6" />
          </g>
          <circle cx="33" cy="32" r="4.2" style={ACCENT} />
        </>
      );
    case "graph-m":
    default:
      return (
        <>
          <path
            d="M16 47 L16 28 A8 8 0 0 1 32 28 L32 47 M32 28 A8 8 0 0 1 48 28 L48 47"
            fill="none"
            stroke="currentColor"
            strokeWidth="4.6"
            strokeLinecap="round"
            strokeLinejoin="round"
          />
          <circle cx="24" cy="20" r="2.6" fill="currentColor" />
          <circle cx="40" cy="20" r="2.6" fill="currentColor" />
          <circle cx="32" cy="28" r="3.7" style={ACCENT} />
        </>
      );
  }
}

export interface LogoProps {
  /** Which mark to render. Defaults to the single-source BRAND_MARK token. */
  variant?: BrandMark;
  /** false = mark only; true = mark + "mnesis" wordmark. */
  lockup?: boolean;
  /** Mark height in px. */
  size?: number;
  /** Accessible name. */
  title?: string;
}

export default function Logo({ variant = BRAND_MARK, lockup = false, size = 28, title = "mnesis" }: LogoProps) {
  const mark = (
    <svg width={size} height={size} viewBox="0 0 64 64" role="img" aria-label={title}>
      <title>{title}</title>
      <Mark variant={variant} />
    </svg>
  );

  if (!lockup) return mark;

  // Wordmark is HTML (not SVG <text>) so it uses the app's loaded Inter font and
  // scales with CSS; color inherits currentColor like the mark.
  return (
    <span style={{ display: "inline-flex", alignItems: "center", gap: `${Math.round(size * 0.28)}px`, color: "currentColor" }}>
      {mark}
      <span
        style={{
          fontSize: `${Math.round(size * 0.62)}px`,
          fontWeight: 600,
          letterSpacing: "-0.01em",
          lineHeight: 1,
          textTransform: "lowercase",
        }}
      >
        mnesis
      </span>
    </span>
  );
}
