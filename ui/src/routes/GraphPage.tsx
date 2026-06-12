import { useQuery } from "@tanstack/react-query";
import { useSearchParams } from "react-router-dom";
import { getGraph } from "../api/endpoints";
import { EntityChip } from "../components/Badges";

// Placeholder graph view: renders the real subgraph data (nodes/edges) as lists.
// G3 replaces this with an interactive canvas; the data wiring + tokens stay.
export default function GraphPage() {
  const [params] = useSearchParams();
  const root = params.get("root") ?? undefined;
  const { data, isLoading, error } = useQuery({
    queryKey: ["graph", root ?? "overview"],
    queryFn: () => getGraph({ root, depth: 2 }),
  });

  return (
    <div className="mx-auto max-w-3xl p-8">
      <header className="mb-6">
        <h1 className="text-xl font-semibold">Graph</h1>
        <p className="text-sm text-muted">
          {root ? `rooted at ${root}` : "overview"}
          {data ? ` · ${data.nodes.length} nodes · ${data.edges.length} edges` : ""}
        </p>
      </header>

      {isLoading && <p className="text-muted">Loading…</p>}
      {error && <p className="text-muted">Could not load the graph — is the API running?</p>}

      {data && (
        <>
          <section className="mb-6">
            <h2 className="mb-2 text-sm font-semibold text-muted">Entities</h2>
            <div className="flex flex-wrap gap-2">
              {data.nodes
                .filter((n) => n.type !== "page")
                .map((n) => (
                  <EntityChip key={n.ref} refName={n.ref} />
                ))}
            </div>
          </section>

          <section>
            <h2 className="mb-2 text-sm font-semibold text-muted">Edges</h2>
            <ul className="space-y-1 text-sm">
              {data.edges.map((e, i) => (
                <li key={i} className={e.demoted ? "text-muted line-through" : "text-muted"}>
                  <span className="text-fg">{e.s}</span> —{e.p}→ <span className="text-fg">{e.o}</span>{" "}
                  <span className="text-xs tabular-nums">({e.confidence.toFixed(2)})</span>
                </li>
              ))}
            </ul>
          </section>
        </>
      )}
    </div>
  );
}
