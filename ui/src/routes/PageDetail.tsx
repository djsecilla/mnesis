import { useQuery } from "@tanstack/react-query";
import { Link, useParams } from "react-router-dom";
import { getPage } from "../api/endpoints";
import { EntityChip, StatusBadge } from "../components/Badges";

export default function PageDetail() {
  const { id = "" } = useParams();
  const { data, isLoading, error } = useQuery({
    queryKey: ["page", id],
    queryFn: () => getPage(id),
    enabled: !!id,
  });

  if (isLoading) return <div className="p-8 text-muted">Loading…</div>;
  if (error || !data) return <div className="p-8 text-muted">Page not found.</div>;

  const fm = data.frontmatter;
  return (
    <div className="mx-auto max-w-3xl p-8">
      <Link to="/pages" className="text-xs text-muted hover:text-accent">
        ← Pages
      </Link>

      <header className="mb-4 mt-2">
        <h1 className="flex items-center text-xl font-semibold">
          {String(fm.title)}
          <StatusBadge status={String(fm.status)} />
          {data.open_contradiction && (
            <span className="ml-2 rounded border border-border px-1.5 py-0.5 text-[10px] uppercase text-accent">
              contradiction
            </span>
          )}
        </h1>
        <p className="mt-1 text-xs text-muted">
          {data.id} · {String(fm.kind)} · confidence {data.confidence.toFixed(3)}
        </p>
      </header>

      {fm.tags?.length > 0 && (
        <div className="mb-5 flex flex-wrap gap-2">
          {fm.tags.map((t) => (
            <EntityChip key={t} refName={t} />
          ))}
        </div>
      )}

      <article className="card whitespace-pre-wrap p-4 text-sm leading-relaxed">{data.body}</article>

      {data.relations.length > 0 && (
        <section className="mt-6">
          <h2 className="mb-2 text-sm font-semibold text-muted">Relations</h2>
          <ul className="space-y-1 text-sm">
            {data.relations.map((r, i) => (
              <li key={i} className="text-muted">
                <span className="text-fg">{r.s}</span> —{r.p}→ <span className="text-fg">{r.o}</span>
              </li>
            ))}
          </ul>
        </section>
      )}

      {(data.supersedes || data.superseded_by) && (
        <section className="mt-6 text-sm text-muted">
          {data.superseded_by && <p>Superseded by {data.superseded_by}</p>}
          {data.supersedes && <p>Supersedes {data.supersedes}</p>}
        </section>
      )}
    </div>
  );
}
