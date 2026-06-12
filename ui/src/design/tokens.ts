// Shared design tokens (consumed by graph, pages, and chat). Color VALUES live in
// CSS custom properties (src/index.css); this module maps entity types/status to
// them so every view styles entities and status the same way.

export const ENTITY_TYPES = [
  "person",
  "project",
  "library",
  "concept",
  "file",
  "decision",
] as const;

export type EntityType = (typeof ENTITY_TYPES)[number];

/** The `type` portion of a `type:value` entity ref (e.g. "library:redis" -> "library"). */
export function entityTypeOf(ref: string): string {
  const i = ref.indexOf(":");
  return i >= 0 ? ref.slice(0, i) : ref;
}

/** A CSS color for an entity type, via the shared custom properties. */
export function entityColor(type: string): string {
  const known = (ENTITY_TYPES as readonly string[]).includes(type);
  return `var(--entity-${known ? type : "page"})`;
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
  const known = (ENTITY_TYPES as readonly string[]).includes(type);
  return resolveCssVar(`--entity-${known ? type : "page"}`);
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
