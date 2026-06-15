// Shared design tokens (consumed by graph, pages, and chat). Color VALUES live in
// CSS custom properties (src/index.css); this module maps entity types/status to
// them so every view styles entities and status the same way.

// ── Brand mark ──────────────────────────────────────────────────────────────
// The active logo variant. This is the SINGLE source of truth: change this one
// line to swap the in-app logo everywhere (rail, lockups, loading states).
// NOTE: when you change this, also update the favicon <link> in index.html to
// the matching /favicon-<variant>.svg (favicons render without CSS, so they are
// referenced by file, not by this constant).
export type BrandMark = "graph-m" | "compounding" | "constellation";
export const BRAND_MARK: BrandMark = "graph-m";

export const ENTITY_TYPES = [
  "person",
  "project",
  "library",
  "concept",
  "file",
  "decision",
] as const;

export type EntityType = (typeof ENTITY_TYPES)[number];

const BUILTIN_TYPES = new Set<string>(ENTITY_TYPES);

/** The `type` portion of a `type:value` entity ref (e.g. "library:redis" -> "library"). */
export function entityTypeOf(ref: string): string {
  const i = ref.indexOf(":");
  return i >= 0 ? ref.slice(0, i) : ref;
}

// Custom entity types (from MNESIS_ENTITY_TYPES) have no curated CSS var, so we
// derive a stable, distinct colour from the type name — a hashed hue at a fixed
// saturation/lightness that reads on both themes. Deterministic: the same type
// always gets the same colour. The structural `page` type keeps its neutral grey.
function hashHue(s: string): number {
  let h = 2166136261;
  for (let i = 0; i < s.length; i++) {
    h ^= s.charCodeAt(i);
    h = Math.imul(h, 16777619);
  }
  return (h >>> 0) % 360;
}

function hslToHex(h: number, s: number, l: number): string {
  s /= 100;
  l /= 100;
  const a = s * Math.min(l, 1 - l);
  const f = (n: number) => {
    const k = (n + h / 30) % 12;
    const c = l - a * Math.max(-1, Math.min(k - 3, 9 - k, 1));
    return Math.round(255 * c).toString(16).padStart(2, "0");
  };
  return `#${f(0)}${f(8)}${f(4)}`;
}

/** Concrete colour for an entity type, including generated colours for custom types. */
function colorForType(type: string): string {
  if (type === "page") return resolveCssVar("--entity-page");
  if (BUILTIN_TYPES.has(type)) return resolveCssVar(`--entity-${type}`);
  return hslToHex(hashHue(type), 62, 53); // custom type -> stable generated colour
}

/** A CSS color for an entity type — a curated var for built-ins, a generated hex otherwise. */
export function entityColor(type: string): string {
  if (type === "page") return "var(--entity-page)";
  if (BUILTIN_TYPES.has(type)) return `var(--entity-${type})`;
  return hslToHex(hashHue(type), 62, 53);
}

export const STATUSES = ["active", "stale"] as const;
export type Status = (typeof STATUSES)[number];

/** stale = muted; active is the default (no extra chrome). */
export function isMutedStatus(status: string): boolean {
  return status !== "active";
}

// ── Runtime token resolution (for the Cytoscape canvas, which needs concrete
//    colors). Still the SAME tokens — read live from the CSS custom properties,
//    so the graph re-themes with the rest of the app on dark/light toggle.

export function resolveCssVar(name: string): string {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

export function entityColorValue(type: string): string {
  return colorForType(type);
}

export interface ThemeColors {
  fg: string;
  muted: string;
  border: string;
  accent: string;
  bg: string;
  elev: string;
}

export function themeColors(): ThemeColors {
  return {
    fg: resolveCssVar("--fg"),
    muted: resolveCssVar("--muted"),
    border: resolveCssVar("--border"),
    accent: resolveCssVar("--accent"),
    bg: resolveCssVar("--bg"),
    elev: resolveCssVar("--bg-elev"),
  };
}
