import { useQuery } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { getSource, listSources } from "../api/endpoints";

export default function SourcesPage() {
  const { data, isLoading, error } = useQuery({ queryKey: ["sources"], queryFn: listSources });
  const [filter, setFilter] = useState("");
  // Selection is URL-driven (?source=<id>) so a page can deep-link to its source.
  const [params, setParams] = useSearchParams();
  const selected = params.get("source");
  const select = (id: string) =>
    setParams(selected === id ? {} : { source: id }, { replace: true });

  const sources = data?.sources ?? [];
  const filtered = useMemo(() => {
    const q = filter.trim().toLowerCase();
    if (!q) return sources;
    return sources.filter((s) => s.id.toLowerCase().includes(q) || s.pages.some((p) => p.title.toLowerCase().includes(q)));
  }, [sources, filter]);

  return (
    <div className="mx-auto max-w-5xl p-8">
      <header className="mb-4 flex items-baseline justify-between">
        <h1 className="text-xl font-semibold">Sources</h1>
        <span className="text-sm text-muted">{filtered.length} of {sources.length}</span>
      </header>
      <p className="mb-4 text-sm text-muted">What you fed in, and what it became. Stored text is already redacted.</p>

      <input
        className="input mb-4 w-full py-1.5"
        placeholder="Filter by source name or page title…"
        value={filter}
        onChange={(e) => setFilter(e.target.value)}
      />

      {isLoading && <p className="text-muted">Loading…</p>}
      {error && <p className="text-muted">Could not load sources — is the API running?</p>}

      <div className="grid grid-cols-1 gap-6 md:grid-cols-[1fr_1fr]">
        {/* list */}
        <ul className="divide-y divide-border">
          {filtered.map((s) => (
            <li key={s.id}>
              <button
                onClick={() => select(s.id)}
                className={`flex w-full flex-col items-start gap-1 py-3 text-left ${selected === s.id ? "text-fg" : ""}`}
              >
                <span className="flex w-full items-center gap-2">
                  <span className="min-w-0 flex-1 truncate text-sm">{s.id}</span>
                  {s.ingested_at && (
                    <span className="shrink-0 text-xs text-muted">{s.ingested_at.slice(0, 10)}</span>
                  )}
                </span>
                <span className="flex flex-wrap gap-2 text-xs">
                  {s.pages.length === 0 ? (
                    <span className="text-muted">no page</span>
                  ) : (
                    s.pages.map((p) => (
                      <span key={p.id} className="text-accent">{p.title}</span>
                    ))
                  )}
                </span>
              </button>
            </li>
          ))}
          {data && filtered.length === 0 && (
            <li className="py-6 text-muted">No sources yet — add one from “Add to Mnesis”.</li>
          )}
        </ul>

        {/* detail */}
        <div className="md:sticky md:top-8 md:self-start">
          {selected ? <SourceDetailPanel id={selected} /> : (
            <p className="rounded-lg border border-dashed border-border px-4 py-8 text-center text-sm text-muted">
              Select a source to see its stored (redacted) text and the page(s) it produced.
            </p>
          )}
        </div>
      </div>
    </div>
  );
}

function SourceDetailPanel({ id }: { id: string }) {
  const { data, isLoading, error } = useQuery({ queryKey: ["source", id], queryFn: () => getSource(id) });
  if (isLoading) return <p className="text-muted">Loading…</p>;
  if (error || !data) return <p className="text-muted">Could not load source.</p>;
  return (
    <div className="card p-4">
      <div className="mb-2 flex items-baseline justify-between gap-2">
        <h2 className="truncate font-medium">{data.id}</h2>
        {data.ingested_at && <span className="shrink-0 text-xs text-muted">{data.ingested_at.slice(0, 19).replace("T", " ")}</span>}
      </div>
      <div className="mb-3 flex flex-wrap gap-2 text-xs">
        {data.pages.length === 0 ? (
          <span className="text-muted">produced no page</span>
        ) : (
          data.pages.map((p) => (
            <Link key={p.id} to={`/pages/${encodeURIComponent(p.id)}`} className="rounded border border-border px-2 py-0.5 text-accent hover:border-accent">
              {p.title}
            </Link>
          ))
        )}
      </div>
      <label className="text-[10px] uppercase tracking-wide text-muted">Stored source (redacted)</label>
      <pre className="mt-1 max-h-[28rem] overflow-auto whitespace-pre-wrap rounded-lg bg-bg p-3 text-xs leading-relaxed text-fg">
        {data.text}
      </pre>
    </div>
  );
}
