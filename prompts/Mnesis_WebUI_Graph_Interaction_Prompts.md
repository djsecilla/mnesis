# Mnesis — Web UI: Graph Interaction Enhancements (Claude Code prompts)

Two prompts for **Claude Code (Opus 4.8)** that refine the graph view from the Web UI playbook (G3): a richer, non-intrusive node detail panel, and focus-on-hover neighborhood highlighting. Run them in order against the existing `ui/` app; both are graph-view changes and share the design tokens.

Same six-part template — **CONTEXT / OBJECTIVE / BUILD / CONSTRAINTS / ACCEPTANCE / ON DONE** — and standing rules: offline with seeded data + `MNESIS_LLM_STUB=1`; conventional commits; `tsc --noEmit` and `npm run build` clean; reuse the shared design tokens; keep `CLAUDE.md`/README in sync if behaviour notes change. Prompts continue the G series.

---

## Prompt G13 — Non-intrusive node detail panel

```
CONTEXT: The graph view (G3) already opens a right side panel when a node is clicked, showing the entity type, declaring pages, and edges. Enhance it: richer entity data (summary, tags, sources, related topics) and make it AS NON-INTRUSIVE AS POSSIBLE - it floats over the graph without resizing it and is trivially dismissible. Locate the existing graph view and entity-panel components rather than creating new ones.

OBJECTIVE: Extend GET /api/entity/{ref} to return panel-ready data in one call, and rework the entity panel into a light floating overlay showing summary, tags, sources, and related topics.

BUILD:
- Backend (thin; reuse store/graph/confidence, no heavy new computation): extend GET /api/entity/{ref} to return:
    { ref, type, confidence?,
      summary,                 // from the highest-confidence ACTIVE declaring page's summary; "" if none
      sources: [ {id, title, kind, confidence, snippet} ],   // declaring/mentioning pages, ranked by confidence
      tags:    [ entity-ref ],                                // co-occurring entity tags across declaring pages, top ~8 by frequency
      related: [ {ref, type, predicate, direction, confidence} ] }  // typed-edge neighbours, demoted excluded by default
  Bound every list. Add/extend a test asserting the enriched shape for a fixture entity.
- Frontend (rework the existing entity panel):
    * Non-intrusive presentation: render as a floating card OVERLAID on the graph - it must NOT resize or reflow the Cytoscape canvas. Anchor right with a small margin, max-width ~320px, max-height with internal scroll, subtle elevation/border from the design tokens, compact. No blocking backdrop - the graph stays fully interactive around/behind it.
    * Don't hide the selection: when the panel opens, gently pan the graph so the selected node is not behind the card (or guarantee the card never covers it).
    * Dismissal (all three): an x button, the Esc key, and a click on empty graph background. Selecting another node replaces the contents; closing clears the node selection.
    * Content, quiet hierarchy with generous spacing:
        - Header: entity name + type badge (type-colored token) + confidence.
        - Summary: 1-3 lines; muted placeholder if absent.
        - Tags: entity chips (type-colored); clicking one focuses that entity in the graph.
        - Sources: the declaring pages as links to /pages/:id, each with kind + confidence - this is the provenance.
        - Related topics: the typed-edge neighbours grouped by predicate; each a chip reading "predicate -> entity" with confidence; clicking selects/focuses that node and updates the panel in place.
    * Reuse the shared tokens (entity-type colors, confidence styling) so it matches pages/chat.

CONSTRAINTS:
- The panel must NOT reflow the graph canvas - overlay, not a layout column. "Non-intrusive" = the graph keeps its size and stays usable.
- One /api/entity call populates the whole panel; no N+1 fetches.
- A tag or related-topic click navigates WITHIN the graph (focus + panel update), not a page reload; a source click opens the page route.
- No new colors or dependencies; reuse tokens.

ACCEPTANCE:
- Clicking a seeded node opens the floating panel with summary, tags, sources (page links), and related topics; the graph canvas does not resize; Esc, x, and background-click all close it; clicking a related topic refocuses the graph and updates the panel; clicking a source opens the page; the backend test passes. tsc clean; build succeeds.

ON DONE: commit ("feat(ui): non-intrusive node detail panel"), report the panel's data sources and the three dismissal paths.
```

---

## Prompt G14 — Focus on hover: highlight neighborhood, dim the rest

```
CONTEXT: The graph (G3, Cytoscape) has no hover feedback. Add focus-on-hover: hovering a node highlights it, its connected edges, and its adjacent nodes, and dims the rest of the graph - making local structure pop without a click. This must coexist cleanly with click-selection and the G13 panel.

OBJECTIVE: Implement transient neighbourhood highlighting on node hover, smooth and harmonized with selection.

BUILD:
- On node 'mouseover': compute the hovered node's closed neighbourhood (node + connectedEdges + adjacent nodes). In a cy.batch(): add a 'dim' class to ALL elements, then remove 'dim' from (or add 'focus' to) the neighbourhood; add a stronger 'hovered' treatment to the node itself (an --accent ring / slight size bump) and emphasize its edges (increased width/opacity, reveal predicate labels). On 'mouseout': clear the classes and restore.
- Styles in the Cytoscape stylesheet using shared tokens: .dim { opacity ~0.12 (edges even lower) }; focused elements full opacity; hovered node ring in --accent; a brief opacity transition (~120ms) for smoothness.
- Harmonize with selection (the G13 panel): hover is TRANSIENT and always restores on mouseout. A selected node carries its own distinct 'selected' style, independent of the dim/focus classes, so selection is never lost during hover; with a node selected and its panel open, hovering elsewhere previews then restores on mouseout WITHOUT closing the panel. Precedence: selection persists; hover overlays temporarily.
- Performance: batch all style mutations; NO layout recomputation and NO refetch on hover; keep it smooth on the bounded graph, no flicker when moving quickly between nodes.
- Pointer-only enhancement: touch/tap still selects (opens the panel) unaffected; respect prefers-reduced-motion by skipping the opacity transition while keeping the state change.

CONSTRAINTS:
- Hover changes visual classes ONLY - never triggers layout or data fetches.
- Must not interfere with click-to-select or the panel; selection state survives hovering.
- Use shared tokens; the dim level must keep the focused subgraph clearly readable (not so dark it disappears).

ACCEPTANCE:
- Hovering a seeded node highlights it + its edges + adjacent nodes and dims the rest; moving away restores fully; rapid hovering across nodes stays smooth with no flicker or layout shift; with a node selected (panel open), hovering elsewhere previews and then restores without closing the panel; tap/click-select still works. tsc clean; build succeeds.

ON DONE: commit ("feat(ui): hover neighbourhood highlighting"), report the hover/selection precedence and the dim/focus class names.
```

---

## Notes for running with Claude Code

- Run G13 then G14 on Opus 4.8, against the existing graph view. G13 has a small backend touch (entity enrichment) plus the panel; G14 is pure frontend Cytoscape.
- The judgement that matters in G13 is "non-intrusive": if the panel reflows or shrinks the graph canvas, it's wrong - it must overlay and leave the graph at full size and interactive. The other tell is N+1 fetching; one `/api/entity` call should fill the whole panel.
- The judgement that matters in G14 is the **hover/selection precedence**: hover is transient and restores on mouseout; selection (and its open panel) must survive hovering. Get that wrong and the panel flickers shut as the pointer moves.
- Both reuse the shared design tokens (entity-type colors, --accent, confidence styling). No new palette.
```
