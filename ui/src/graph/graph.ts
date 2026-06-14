// Cytoscape element building + stylesheet + layout, driven by the shared design
// tokens (read live from CSS vars). No per-view color forks.
import type { ElementDefinition, StylesheetStyle } from "cytoscape";
import type { GraphData, GraphEdge, GraphNode } from "../api/types";
import { entityColorValue, themeColors } from "../design/tokens";

/** Cap on simultaneous nodes — keeps the canvas legible (see "collapse distant"). */
export const MAX_NODES = 80;

export function nodeSize(degree: number): number {
  return Math.min(54, 20 + degree * 6);
}

export function edgeWidth(confidence: number): number {
  return 1 + confidence * 4;
}

export function edgeOpacity(confidence: number): number {
  return 0.25 + confidence * 0.6;
}

export function edgeId(e: { s: string; p: string; o: string }): string {
  return `${e.s}|${e.p}|${e.o}`;
}

/**
 * Display label: the value half of a `type:value` ref. The type is already
 * encoded by node color, so dropping the prefix halves label width and noise
 * (`library:redis` -> `redis`). The full ref stays the node id and shows in the
 * detail panel on click.
 */
export function shortLabel(ref: string): string {
  const i = ref.indexOf(":");
  return i >= 0 ? ref.slice(i + 1) : ref;
}

export function nodeElement(n: GraphNode): ElementDefinition {
  return {
    group: "nodes",
    data: {
      id: n.ref,
      label: shortLabel(n.ref),
      ref: n.ref,
      type: n.type,
      degree: n.degree,
      color: entityColorValue(n.type),
      size: nodeSize(n.degree),
    },
  };
}

export function edgeElement(e: GraphEdge): ElementDefinition {
  return {
    group: "edges",
    data: {
      id: edgeId(e),
      source: e.s,
      target: e.o,
      p: e.p,
      confidence: e.confidence,
      assertion_count: e.assertion_count,
      width: edgeWidth(e.confidence),
      opacity: edgeOpacity(e.confidence),
      source_pages: e.source_pages,
    },
    classes: e.demoted ? "demoted" : "",
  };
}

export function toElements(graph: GraphData): ElementDefinition[] {
  return [...graph.nodes.map(nodeElement), ...graph.edges.map(edgeElement)];
}

export function stylesheet(): StylesheetStyle[] {
  const c = themeColors();
  // Respect prefers-reduced-motion: keep the state change, skip the animation.
  const reduced =
    typeof window !== "undefined" &&
    window.matchMedia &&
    window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  const dur = (reduced ? "0s" : "120ms") as unknown as number;
  return [
    {
      selector: "node",
      style: {
        "background-color": "data(color)",
        width: "data(size)",
        height: "data(size)",
        label: "data(label)",
        // Muted so labels recede and the colored structure dominates; a halo
        // (text-outline in the bg color) keeps them legible over edges without a
        // boxy background. Truncated so long refs (e.g. page ids) never sprawl.
        color: c.muted,
        "font-size": 8,
        "font-weight": 500,
        "font-family": "Inter, system-ui, sans-serif",
        "text-valign": "bottom",
        "text-margin-y": 3,
        "text-max-width": "84px",
        ["text-wrap" as string]: "ellipsis",
        "text-outline-width": 1.6,
        "text-outline-color": c.bg,
        "text-outline-opacity": 1,
        // Auto-declutter: a label is hidden once it would render below this many
        // on-screen px. After "fit", a small graph zooms in (labels show) while a
        // dense one zooms out (labels hide until you zoom into a region).
        "min-zoomed-font-size": 8,
        "border-width": 0,
        "transition-property": "opacity",
        "transition-duration": dur,
      },
    },
    {
      // Selected/focused: ring + label always legible regardless of zoom.
      selector: "node:selected",
      style: {
        "border-width": 3,
        "border-color": c.accent,
        color: c.fg,
        "min-zoomed-font-size": 0,
      },
    },
    {
      selector: "edge",
      style: {
        width: "data(width)",
        // @types/cytoscape under-types opacity (rejects data() mappers cytoscape accepts).
        opacity: "data(opacity)" as unknown as number,
        "line-color": c.border,
        "target-arrow-color": c.border,
        "target-arrow-shape": "triangle",
        "arrow-scale": 0.8,
        "curve-style": "bezier",
        label: "data(p)",
        "font-size": 7,
        color: c.muted,
        "text-rotation": "autorotate",
        "text-outline-width": 1.5,
        "text-outline-color": c.bg,
        "text-outline-opacity": 1,
        // Predicate labels are the noisiest at scale: show them only when zoomed
        // in close, or on hover (edge.hover-edge drops this floor to 0).
        "min-zoomed-font-size": 13,
        "transition-property": "opacity, width",
        "transition-duration": dur,
      },
    },
    {
      selector: "edge.demoted",
      style: { "line-style": "dashed", opacity: 0.4 },
    },
    {
      selector: ".hidden",
      style: { display: "none" },
    },
    {
      selector: ".dim",
      style: { opacity: 0.1, "text-opacity": 0.1 },
    },
    {
      selector: "node.hl",
      style: { "border-width": 3, "border-color": c.accent, opacity: 1 },
    },
    {
      selector: "edge.hl",
      style: {
        "line-color": c.accent,
        "target-arrow-color": c.accent,
        width: 4,
        opacity: 1,
      },
    },

    // ── Focus-on-hover (transient; distinct from impact's .dim/.hl and from
    //    :selected, so selection is never lost while hovering). ──────────────
    // Everything outside the hovered neighbourhood fades back but stays readable.
    {
      selector: ".hover-dim",
      style: { opacity: 0.12, "text-opacity": 0.12 },
    },
    {
      selector: "edge.hover-dim",
      style: { opacity: 0.05, "text-opacity": 0 },
    },
    // The hovered node: accent ring, on top, full opacity, label always shown.
    {
      selector: "node.hovered",
      style: {
        "border-width": 4,
        "border-color": c.accent,
        color: c.fg,
        opacity: 1,
        "text-opacity": 1,
        "min-zoomed-font-size": 0,
        "z-index": 999,
      },
    },
    // Its edges: thicker, full opacity, predicate label revealed.
    {
      selector: "edge.hover-edge",
      style: {
        "line-color": c.accent,
        "target-arrow-color": c.accent,
        width: 4,
        opacity: 1,
        "text-opacity": 1,
        "min-zoomed-font-size": 0,
        "z-index": 998,
      },
    },
  ];
}

export const FCOSE_LAYOUT = {
  name: "fcose",
  animate: true,
  animationDuration: 350,
  randomize: false,
  fit: true,
  padding: 40,
  nodeRepulsion: 6000,
  idealEdgeLength: 110,
  nodeSeparation: 80,
} as const;
