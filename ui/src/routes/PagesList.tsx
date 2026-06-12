import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { listPages } from "../api/endpoints";
import { StatusBadge } from "../components/Badges";

export default function PagesList() {
  const { data, isLoading, error } = useQuery({
    queryKey: ["pages"],
    queryFn: () => listPages(),
  });

  return (
    <div className="mx-auto max-w-3xl p-8">
      <header className="mb-6 flex items-baseline justify-between">
        <h1 className="text-xl font-semibold">Pages</h1>
        <span className="text-sm text-muted">{data ? `${data.total} total` : ""}</span>
      </header>

      {isLoading && <p className="text-muted">Loading…</p>}
      {error && <p className="text-muted">Could not load pages — is the API running?</p>}

      <ul className="divide-y divide-border">
        {data?.pages.map((p) => (
          <li key={p.id}>
            <Link
              to={`/pages/${encodeURIComponent(p.id)}`}
              className="flex items-center gap-3 py-3 hover:text-accent"
            >
              <span className="min-w-0 flex-1">
                <span className="flex items-center">
                  <span className="truncate">{p.title}</span>
                  <StatusBadge status={p.status} />
                </span>
                <span className="block truncate text-xs text-muted">{p.id}</span>
              </span>
              <span className="shrink-0 text-xs text-muted">{p.kind}</span>
              <span className="w-14 shrink-0 text-right text-xs tabular-nums text-muted">
                {p.confidence.toFixed(2)}
              </span>
            </Link>
          </li>
        ))}
        {data && data.pages.length === 0 && (
          <li className="py-6 text-muted">No pages yet. Ingest a source to get started.</li>
        )}
      </ul>
    </div>
  );
}
