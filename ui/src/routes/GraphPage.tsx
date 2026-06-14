import { useQuery } from "@tanstack/react-query";
import cytoscape, { type Core, type EventObject, type NodeSingular } from "cytoscape";
import fcose from "cytoscape-fcose";
import { useEffect, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { getGraph, getImpact } from "../api/endpoints";
import type { GraphData, ImpactResponse } from "../api/types";
import { entityColor, entityColorValue } from "../design/tokens";
import { BrandSplash } from "../components/Logo";
import GraphPanel from "../graph/GraphPanel";
import {
  FCOSE_LAYOUT,
  MAX_NODES,
  edgeElement,
  edgeId,
  nodeElement,
  stylesheet,
  toElements,
} from "../graph/graph";

let fcoseRegistered = false;
function ensureFcose() {
  if (!fcoseRegistered) {
    cytoscape.use(fcose);
    fcoseRegistered = true;
  }
}

export default function GraphPage() {
  const containerRef = useRef<HTMLDivElement>(null);
  const cyRef = useRef<Core | null>(null);

  const [searchParams, setSearchParams] = useSearchParams();
  const root = searchParams.get("root") || undefined;
  const depth = Number(searchParams.get("depth") || 2);
  const showDemoted = searchParams.get("demoted") === "1";

  const [selected, setSelected] = useState<string | null>(null);
  const [impactRef, setImpactRef] = useState<string | null>(null);
  const [impactData, setImpactData] = useState<ImpactResponse | undefined>();
  const [impactLoading, setImpactLoading] = useState(false);
  const [overCap, setOverCap] = useState(false);
  const [searchInput, setSearchInput] = useState("");

  const base = useQuery({
    queryKey: ["graph", root ?? "overview", depth],
    queryFn: () => getGraph({ root, depth, include_demoted: true }),
  });

  // ── cytoscape lifecycle ───────────────────────────────────────────────────
  useEffect(() => {
    if (!containerRef.current) return;
    ensureFcose();
    const cy = cytoscape({
      container: containerRef.current,
      style: stylesheet(),
      elements: [],
      minZoom: 0.2,
      maxZoom: 2.5,
      wheelSensitivity: 0.25,
    });
    cy.on("tap", "node", (e: EventObject) => {
      const id = e.target.id();
      setSelected(id);
      setImpactRef((prev) => (prev && prev !== id ? null : prev));
    });
    cy.on("dbltap", "node", (e: EventObject) => expand(e.target.id()));
    cy.on("tap", (e: EventObject) => {
      if (e.target === cy) {
        setSelected(null);
        setImpactRef(null);
      }
    });

    // ── Focus-on-hover (pointer only; mouseover/mouseout never fire on touch) ──
    // Visual classes ONLY — no layout, no refetch. Transient: always restores on
    // mouseout. A short coalescing timer means moving directly between nodes never
    // flashes back to the full graph (no flicker). Selection (:selected) is kept
    // un-dimmed so the panel's node stays visible while previewing elsewhere.
    let hoverClearTimer: ReturnType<typeof setTimeout> | null = null;
    function applyHover(node: NodeSingular) {
      cy.batch(() => {
        cy.elements().removeClass("hovered hover-edge").addClass("hover-dim");
        node.closedNeighborhood().removeClass("hover-dim"); // node + adj nodes + edges
        node.addClass("hovered");
        node.connectedEdges().removeClass("hover-dim").addClass("hover-edge");
        cy.elements(":selected").removeClass("hover-dim"); // selection persists
      });
    }
    function clearHover() {
      cy.batch(() => cy.elements().removeClass("hover-dim hovered hover-edge"));
    }
    cy.on("mouseover", "node", (e: EventObject) => {
      if (hoverClearTimer) {
        clearTimeout(hoverClearTimer);
        hoverClearTimer = null;
      }
      applyHover(e.target as NodeSingular);
    });
    cy.on("mouseout", "node", () => {
      if (hoverClearTimer) clearTimeout(hoverClearTimer);
      hoverClearTimer = setTimeout(clearHover, 30);
    });

    cyRef.current = cy;

    // Re-theme the canvas when the app theme changes (same shared tokens).
    const obs = new MutationObserver(() => restyle());
    obs.observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });

    return () => {
      if (hoverClearTimer) clearTimeout(hoverClearTimer);
      obs.disconnect();
      cy.destroy();
      cyRef.current = null;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  function restyle() {
    const cy = cyRef.current;
    if (!cy) return;
    cy.nodes().forEach((n) => {
      n.data("color", entityColorValue(String(n.data("type"))));
    });
    cy.style(stylesheet());
    applyDemoted();
  }

  function applyDemoted() {
    cyRef.current?.edges(".demoted").toggleClass("hidden", !showDemoted);
  }

  function runLayout() {
    cyRef.current?.layout({ ...FCOSE_LAYOUT }).run();
  }

  function enforceCap() {
    const cy = cyRef.current;
    setOverCap(!!cy && cy.nodes().length > MAX_NODES);
  }

  function centerOn(ref: string) {
    const cy = cyRef.current;
    if (!cy) return;
    const n = cy.getElementById(ref);
    if (n.nonempty()) {
      cy.elements().unselect();
      n.select();
      cy.animate({ center: { eles: n }, zoom: Math.max(cy.zoom(), 1) }, { duration: 300 });
    }
  }

  function mergeGraph(g: GraphData) {
    const cy = cyRef.current;
    if (!cy) return;
    cy.batch(() => {
      for (const n of g.nodes) if (cy.getElementById(n.ref).empty()) cy.add(nodeElement(n));
      for (const e of g.edges) if (cy.getElementById(edgeId(e)).empty()) cy.add(edgeElement(e));
    });
    applyDemoted();
    runLayout();
    enforceCap();
  }

  async function expand(ref: string) {
    const g = await getGraph({ root: ref, depth: 1, include_demoted: true });
    mergeGraph(g);
    setSelected(ref);
  }

  async function focusEntity(refRaw: string) {
    const ref = refRaw.trim();
    if (!ref) return;
    const cy = cyRef.current;
    if (cy && cy.getElementById(ref).nonempty()) {
      setSelected(ref);
      centerOn(ref);
    } else {
      const g = await getGraph({ root: ref, depth: 1, include_demoted: true });
      mergeGraph(g);
      setSelected(ref);
      centerOn(ref);
    }
  }

  function collapseDistant() {
    const cy = cyRef.current;
    if (!cy) return;
    const focusId = selected ?? root;
    const focus = focusId ? cy.getElementById(focusId) : cy.nodes().length ? cy.nodes()[0] : null;
    if (!focus || focus.empty()) return;
    const oneHop = focus.closedNeighborhood().nodes();
    const twoHop = oneHop.neighborhood().nodes().union(oneHop);
    cy.nodes().difference(twoHop).remove();
    runLayout();
    enforceCap();
  }

  // ── reset elements when the base view (root/depth) changes ────────────────
  useEffect(() => {
    const cy = cyRef.current;
    if (!cy || !base.data) return;
    cy.elements().remove();
    cy.add(toElements(base.data));
    applyDemoted();
    runLayout();
    enforceCap();
    if (root) {
      setSelected(root);
      setTimeout(() => centerOn(root), 360);
    } else {
      setSelected(null);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [base.data]);

  useEffect(() => {
    applyDemoted();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [showDemoted]);

  // Keep the selected node clear of the floating panel (anchored top-right): if it
  // would sit under the card, gently pan it left. Centered/focused nodes land at
  // mid-canvas (clear of the right card), so this only nudges tapped right-side nodes.
  useEffect(() => {
    const cy = cyRef.current;
    const cont = containerRef.current;
    if (!cy || !cont || !selected) return;
    const id = setTimeout(() => {
      const n = cy.getElementById(selected);
      if (n.empty()) return;
      const pos = n.renderedPosition();
      const cardLeft = cont.clientWidth - 320 - 24; // panel width + right margin
      if (pos.x > cardLeft - 30) {
        cy.animate({ panBy: { x: -(pos.x - (cardLeft - 90)), y: 0 } }, { duration: 250 });
      }
    }, 380); // after any center/focus animation settles
    return () => clearTimeout(id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selected]);

  // ── impact mode ───────────────────────────────────────────────────────────
  useEffect(() => {
    const cy = cyRef.current;
    if (!cy) return;
    if (!impactRef) {
      cy.elements().removeClass("dim hl");
      setImpactData(undefined);
      return;
    }
    let cancelled = false;
    setImpactLoading(true);
    (async () => {
      try {
        const [g, imp] = await Promise.all([
          getGraph({ root: impactRef, depth: 3, include_demoted: false }),
          getImpact(impactRef, 3),
        ]);
        if (cancelled) return;
        mergeGraph(g);
        highlightImpact(imp, impactRef);
        setImpactData(imp);
      } finally {
        if (!cancelled) setImpactLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [impactRef]);

  function highlightImpact(imp: ImpactResponse, rootRef: string) {
    const cy = cyRef.current;
    if (!cy) return;
    const nodes = new Set<string>([rootRef]);
    const pairs: [string, string][] = [];
    for (const a of imp.affected) {
      a.path.forEach((r) => nodes.add(r));
      for (let i = 0; i < a.path.length - 1; i++) pairs.push([a.path[i], a.path[i + 1]]);
    }
    cy.batch(() => {
      cy.elements().addClass("dim").removeClass("hl");
      nodes.forEach((r) => {
        const n = cy.getElementById(r);
        if (n.nonempty()) n.removeClass("dim").addClass("hl");
      });
      cy.edges().forEach((e) => {
        const s = e.source().id();
        const t = e.target().id();
        if (pairs.some(([x, y]) => (s === x && t === y) || (s === y && t === x))) {
          e.removeClass("dim").addClass("hl");
        }
      });
    });
  }

  const loading = base.isLoading || base.isFetching;
  const empty = base.data && base.data.nodes.length === 0;
  // Entity types present in the current view (for the legend).
  const presentTypes = Array.from(new Set((base.data?.nodes ?? []).map((n) => n.type))).sort();

  return (
    <div className="relative h-full">
        {/* toolbar */}
        <div className="absolute left-3 top-3 z-10 flex items-center gap-2">
          <input
            className="w-56 rounded-lg border border-border bg-elev/90 px-3 py-1.5 text-sm placeholder:text-muted focus:border-accent focus:outline-none"
            placeholder="Focus entity (e.g. library:redis)…"
            value={searchInput}
            onChange={(e) => setSearchInput(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") {
                focusEntity(searchInput);
                setSearchInput("");
              }
            }}
          />
          <button
            onClick={() =>
              setSearchParams((p) => {
                const next = new URLSearchParams(p);
                if (showDemoted) next.delete("demoted");
                else next.set("demoted", "1");
                return next;
              })
            }
            className={`rounded-lg border border-border px-3 py-1.5 text-xs ${
              showDemoted ? "bg-elev text-fg" : "text-muted"
            }`}
            title="Toggle demoted (stale-only) edges"
          >
            demoted
          </button>
          <button
            onClick={() => base.refetch()}
            className="rounded-lg border border-border px-3 py-1.5 text-xs text-muted hover:text-fg"
            title="Refresh"
          >
            ↻
          </button>
          {overCap && (
            <button
              onClick={collapseDistant}
              className="rounded-lg bg-accent px-3 py-1.5 text-xs text-accent-fg"
              title="Too many nodes — keep the focused neighborhood"
            >
              collapse distant
            </button>
          )}
        </div>

        {loading && (
          <div className="pointer-events-none absolute inset-0 z-0 flex items-center justify-center text-muted">
            Loading graph…
          </div>
        )}
        {empty && !loading && (
          <div className="absolute inset-0 z-0">
            <BrandSplash tagline="No graph yet — ingest a source and it takes shape here." />
          </div>
        )}
        {base.error && (
          <div className="absolute inset-0 z-0 flex items-center justify-center text-muted">
            Could not load the graph — is the API running?
          </div>
        )}

        <div ref={containerRef} className="h-full w-full" />

        {/* Legend: color → entity type. Labels drop the `type:` prefix (color
            carries the type), so this keeps the encoding intuitive. Only the
            types actually present are shown, so it never adds noise. */}
        {!empty && !loading && presentTypes.length > 0 && (
          <div className="pointer-events-none absolute bottom-3 left-3 z-10 flex flex-wrap gap-x-3 gap-y-1 rounded-lg border border-border bg-elev/80 px-2.5 py-1.5 text-[10px] text-muted backdrop-blur">
            {presentTypes.map((t) => (
              <span key={t} className="inline-flex items-center gap-1">
                <span className="h-2 w-2 rounded-full" style={{ background: entityColor(t) }} />
                {t}
              </span>
            ))}
          </div>
        )}

        {/* Floating overlay — sits over the canvas; does not reflow it. */}
        {selected && (
          <GraphPanel
            refName={selected}
            onClose={() => {
              setSelected(null);
              setImpactRef(null);
            }}
            onExpand={expand}
            onFocus={focusEntity}
            impactActive={impactRef === selected}
            onToggleImpact={() => setImpactRef((r) => (r === selected ? null : selected))}
            impact={impactData}
            impactLoading={impactLoading}
          />
        )}
    </div>
  );
}
