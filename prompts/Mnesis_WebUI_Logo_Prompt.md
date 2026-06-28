# Mnesis — Web UI: Persistent Brand Logo (Claude Code prompt)

A single prompt for **Claude Code (Opus 4.8)** that adds the Mnesis logo to the web UI so it is always present, themed through the existing accent token, and swappable among the three marks with a one-line change. Paste it as one turn against the `ui/` app from the Web UI playbook.

**Swapping later:** change the `BRAND_MARK` constant (in-app logo) and the favicon `<link>` in `index.html` (browser tab). Variants: `graph-m`, `compounding`, `constellation`.

---

```
CONTEXT: The Mnesis web UI (ui/ — React 18 + TypeScript + Vite + Tailwind, with a persistent slim left rail and a single accent design token --accent set on :root, from the Web UI playbook) currently has no brand mark. Add the logo so it is always on screen, adapts to the dark/light themes, uses the existing accent token, and can be swapped among three marks from one place.

OBJECTIVE: Create a reusable Logo component, place the mark at the top of the left rail on every route (as a home link), wire a matching favicon, and make the active variant a single-source, one-line choice.

BUILD:
- ui/src/components/Logo.tsx — a presentational, dependency-free component rendering inline SVG.
    * Props: variant?: 'graph-m' | 'compounding' | 'constellation' (defaults to the BRAND_MARK constant below); lockup?: boolean (false = mark only; true = mark + "mnesis" wordmark); size?: number (px, mark height; default 28); title?: string for a11y (default "mnesis").
    * Ink uses currentColor so the mark inherits the surrounding text color and inverts with the theme automatically.
    * The accent (memory) node uses the --accent token. IMPORTANT: CSS var() does NOT resolve inside an SVG fill ATTRIBUTE, so apply the accent via CSS — a class or inline style, e.g. style={{ fill: 'var(--accent)' }} (or a .logo-accent { fill: var(--accent) } rule). currentColor in attributes is fine.
    * Mark geometry (viewBox 0 0 64 64), three variants — accent elements marked /*accent*/ get the token fill, everything else currentColor:

      graph-m:
        <path d="M16 47 L16 28 A8 8 0 0 1 32 28 L32 47 M32 28 A8 8 0 0 1 48 28 L48 47" fill="none" stroke="currentColor" stroke-width="4.6" stroke-linecap="round" stroke-linejoin="round"/>
        <circle cx="24" cy="20" r="2.6" fill="currentColor"/>
        <circle cx="40" cy="20" r="2.6" fill="currentColor"/>
        <circle cx="32" cy="28" r="3.7" /*accent*/ />

      compounding:
        <circle cx="32" cy="32" r="24" fill="none" stroke="currentColor" stroke-width="2.4" opacity="0.30"/>
        <circle cx="32" cy="32" r="17" fill="none" stroke="currentColor" stroke-width="2.9" opacity="0.55"/>
        <circle cx="32" cy="32" r="10" fill="none" stroke="currentColor" stroke-width="3.4"/>
        <circle cx="32" cy="22" r="2.6" /*accent*/ />
        <circle cx="49" cy="32" r="2.2" fill="currentColor"/>
        <circle cx="15" cy="49" r="2"   fill="currentColor"/>
        <circle cx="32" cy="32" r="5"   /*accent*/ />

      constellation:
        <g stroke="currentColor" stroke-width="1.7" opacity="0.4">
          <line x1="33" y1="32" x2="20" y2="21"/><line x1="33" y1="32" x2="46" y2="18"/><line x1="33" y1="32" x2="49" y2="42"/>
          <line x1="33" y1="32" x2="23" y2="46"/><line x1="20" y1="21" x2="23" y2="46"/><line x1="46" y1="18" x2="49" y2="42"/>
        </g>
        <g fill="currentColor"><circle cx="20" cy="21" r="3"/><circle cx="46" cy="18" r="2.6"/><circle cx="49" cy="42" r="3"/><circle cx="23" cy="46" r="2.6"/></g>
        <circle cx="33" cy="32" r="4.2" /*accent*/ />

    * Lockup: render the mark SVG, then the wordmark as a styled HTML <span>mnesis</span> (NOT SVG <text>) so it uses the app's loaded Inter font and scales with CSS — inline-flex, vertically centered, lowercase, font-weight 600, letter-spacing -0.01em, color inherits currentColor, gap ~0.5em scaled to size. Set the SVG width/height from `size`, viewBox 0 0 64 64, with role="img" and <title>{title}</title>.
- Single source of truth: add BRAND_MARK (the default variant) to the existing design-tokens/theme module (wherever G2 put --accent and the tokens). Logo defaults to it; changing this one value swaps the in-app logo everywhere.
- Place in the app shell (the left-rail component): the mark at the very top, above the nav icons, wrapped in a router Link to the default route ("/graph" or the app's home), aria-label "mnesis — home", size tuned to the rail width. Do not add a top banner or restyle the rail — slot into the existing layout. Use the lockup variant only where there is horizontal room (e.g. a wider header, or empty/loading/auth states) — mark-only in the slim rail.
- Favicon (browser tab is "always present" too): add the three marks as static SVGs in ui/public/ (favicon-graph-m.svg, favicon-compounding.svg, favicon-constellation.svg). These have NO CSS context, so bake literal colors: accent #F2762E, and ink #E8E8E8 (reads on both light and dark tabs). Reference the variant matching BRAND_MARK via <link rel="icon" type="image/svg+xml" href="/favicon-<variant>.svg"> in index.html, plus a 32x32 PNG fallback (<link rel="alternate icon" ...>) for older browsers.

CONSTRAINTS:
- One accent source: the in-app logo's accent reads --accent (via class/style, never a var() fill attribute); the only literal hex allowed is inside the public favicon files, which render without CSS.
- No hardcoded ink color in the component — currentColor only, so theme switching needs no logo change.
- No new dependencies; the component is pure/presentational.
- Swapping the in-app logo must be a one-line change (BRAND_MARK). Note in a comment that the favicon link must be updated to match when swapping.
- Keep it static — no animation.

ACCEPTANCE:
- The mark renders at the top of the left rail on every route, links home, and inverts correctly between dark and light (ink follows text color; the accent node stays orange in both).
- Setting BRAND_MARK to each of 'graph-m' | 'compounding' | 'constellation' swaps the in-app logo with no other code change; <Logo lockup /> shows the mark + "mnesis" in the app's Inter, baseline-aligned.
- The favicon appears in the browser tab. `tsc --noEmit` is clean and `npm run build` succeeds.
- (If a component-test setup exists) a Logo test asserts each variant renders an <svg> with a <title> and that exactly the accent nodes carry the accent style.

ON DONE: run the type-check/build, commit ("feat(ui): persistent brand logo and favicon"), and report where BRAND_MARK lives, the one line to change to swap variants, and the favicon <link> line to keep in sync.
```
