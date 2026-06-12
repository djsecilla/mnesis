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

export function nodeElement(n: GraphNode): ElementDefinition {
  return {
    group: "nodes",
    data: {
      id: n.ref,
      label: n.ref,
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
  return [
    {
      selector: "node",
      style: {
        "background-color": "data(color)",
        width: "data(size)",
        height: "data(size)",
        label: "data(label)",
        color: c.fg,
        "font-size": 10,
        "font-family": "Inter, system-ui, sans-serif",
        "text-valign": "bottom",
        "text-margin-y": 4,
        "min-zoomed-font-size": 8,
        "border-width": 0,
      },
    },
    {
      selector: "node:selected",
      style: { "border-width": 3, "border-color": c.accent },
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
        "font-size": 8,
        color: c.muted,
        "text-rotation": "autorotate",
        "text-background-color": c.bg,
        "text-background-opacity": 0.85,
        "text-background-padding": "1",
        "min-zoomed-font-size": 7,
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
