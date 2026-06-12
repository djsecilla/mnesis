import { useQuery } from "@tanstack/react-query";
import cytoscape, { type Core, type EventObject } from "cytoscape";
import fcose from "cytoscape-fcose";
import { useEffect, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { getGraph, getImpact } from "../api/endpoints";
import type { GraphData, ImpactResponse } from "../api/types";
import { entityColorValue } from "../design/tokens";
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
    cyRef.current = cy;

    // Re-theme the canvas when the app theme changes (same shared tokens).
    const obs = new MutationObserver(() => restyle());
    obs.observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });

    return () => {
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

  return (
    <div className="flex h-full">
      <div className="relative flex-1">
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
          <div className="absolute inset-0 z-0 flex items-center justify-center text-muted">
            No graph yet — ingest sources, then run rebuild.
          </div>
        )}
        {base.error && (
          <div className="absolute inset-0 z-0 flex items-center justify-center text-muted">
            Could not load the graph — is the API running?
          </div>
        )}

        <div ref={containerRef} className="h-full w-full" />
      </div>

      {selected && (
        <GraphPanel
          refName={selected}
          onClose={() => {
            setSelected(null);
            setImpactRef(null);
          }}
          onExpand={expand}
          impactActive={impactRef === selected}
          onToggleImpact={() => setImpactRef((r) => (r === selected ? null : selected))}
          impact={impactData}
          impactLoading={impactLoading}
        />
      )}
    </div>
  );
}
