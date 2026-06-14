import { useQueries, useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import { getEntity, getPage } from "../api/endpoints";
import { EntityChip, KindBadge, RelationChip, StatusBadge } from "../components/Badges";
import { ConfidenceMeter } from "../components/Confidence";
import Markdown from "../components/Markdown";

function PageLink({ id }: { id: string }) {
  return (
    <Link to={`/pages/${encodeURIComponent(id)}`} className="text-accent hover:underline">
      {id}
    </Link>
  );
}

/** Link a source ref to its full stored text (Sources view), or out if it's a URL. */
function SourceRef({ refName }: { refName: string }) {
  const isUrl = /^https?:\/\//i.test(refName);
  if (isUrl) {
    return (
      <a href={refName} target="_blank" rel="noreferrer" className="text-accent hover:underline">
        {refName}
      </a>
    );
  }
  return (
    <Link to={`/sources?source=${encodeURIComponent(refName)}`} className="text-accent hover:underline">
      {refName}
    </Link>
  );
}

function CopyButton({ text }: { text: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <button
      onClick={async () => {
        try {
          await navigator.clipboard.writeText(text);
          setCopied(true);
          setTimeout(() => setCopied(false), 1500);
        } catch {
          /* clipboard unavailable — no-op */
        }
      }}
      className="rounded border border-border px-2 py-0.5 text-xs text-muted hover:border-accent hover:text-fg"
    >
      {copied ? "Copied" : "Copy"}
    </button>
  );
}

export default function PageDetail() {
  const { id = "" } = useParams();
  const [showRaw, setShowRaw] = useState(false);
  const { data, isLoading, error } = useQuery({
    queryKey: ["page", id],
    queryFn: () => getPage(id),
    enabled: !!id,
  });

  // Edge confidences for the relation chips: look up each distinct subject entity.
  const subjects = Array.from(new Set((data?.relations ?? []).map((r) => r.s)));
  const entityResults = useQueries({
    queries: subjects.map((s) => ({
      queryKey: ["entity", s],
      queryFn: () => getEntity(s),
      staleTime: 30_000,
    })),
  });
  const edgeConf = new Map<string, number>();
  entityResults.forEach((r) => r.data?.edges.forEach((e) => edgeConf.set(`${e.s}|${e.p}|${e.o}`, e.confidence)));

  if (isLoading) return <div className="p-8 text-muted">Loading…</div>;
  if (error || !data) return <div className="p-8 text-muted">Page not found.</div>;

  const fm = data.frontmatter;
  const kind = String(fm.kind);
  const question = fm.question ? String(fm.question) : "";

  return (
    <div className="mx-auto max-w-3xl p-8">
      <Link to="/pages" className="text-xs text-muted hover:text-accent">
        ← Pages
      </Link>

      {/* Lifecycle banners */}
      {data.superseded_by && (
        <div className="mt-3 rounded-lg border border-accent/60 bg-accent/10 px-4 py-2 text-sm">
          This page was <span className="font-medium">superseded</span> by{" "}
          <PageLink id={data.superseded_by} />.
        </div>
      )}
      {data.open_contradiction && (
        <div className="mt-3 rounded-lg border border-border bg-elev px-4 py-2 text-sm text-muted">
          ⚠ Open contradiction
          {data.contradicts.length > 0 && (
            <>
              {" "}with{" "}
              {data.contradicts.map((c, i) => (
                <span key={c}>
                  {i > 0 && ", "}
                  <PageLink id={c} />
                </span>
              ))}
            </>
          )}{" "}
          — under review.
        </div>
      )}

      {/* Compact header */}
      <header className="mb-3 mt-3">
        <h1 className="text-2xl font-semibold tracking-tight">{String(fm.title)}</h1>
        <div className="mt-2 flex flex-wrap items-center gap-3 text-xs text-muted">
          <KindBadge kind={kind} />
          <StatusBadge status={String(fm.status)} />
          <ConfidenceMeter value={data.confidence} breakdown={data.breakdown} />
          {Array.isArray(fm.sources) && fm.sources.length > 0 && (
            <span className="flex flex-wrap items-center gap-x-1.5">
              {/* digest sources are the page ids it was synthesized from; fact/note
                  sources are the original ingested source files. */}
              {kind === "digest" ? "synthesized from:" : "sources:"}
              {(fm.sources as string[]).map((s, i) => (
                <span key={s}>
                  {i > 0 && <span className="text-muted/50">·</span>}{" "}
                  {kind === "digest" ? <PageLink id={s} /> : <SourceRef refName={s} />}
                </span>
              ))}
            </span>
          )}
          <span>confirmed {String(fm.last_confirmed).slice(0, 10)}</span>
          {data.id !== String(fm.title) && <span className="text-muted/70">{data.id}</span>}
        </div>
      </header>

      {/* Digest: originating question, distinctly */}
      {kind === "digest" && question && (
        <blockquote className="mb-4 border-l-2 border-accent pl-3 text-sm text-muted">
          <span className="text-[10px] uppercase tracking-wide">question</span>
          <p className="text-fg">{question}</p>
        </blockquote>
      )}

      {/* Entity chips */}
      {fm.tags?.length > 0 && (
        <div className="mb-3 flex flex-wrap gap-2">
          {fm.tags.map((t) => (
            <EntityChip key={t} refName={t} />
          ))}
        </div>
      )}

      {/* Relation chips */}
      {data.relations.length > 0 && (
        <div className="mb-5 flex flex-wrap gap-2">
          {data.relations.map((r, i) => (
            <RelationChip
              key={i}
              s={r.s}
              p={r.p}
              o={r.o}
              confidence={edgeConf.get(`${r.s}|${r.p}|${r.o}`)}
            />
          ))}
        </div>
      )}

      {/* Body — rendered prose, or the full canonical .md (frontmatter + body) */}
      <div className="mb-3 flex items-center gap-2">
        <div className="inline-flex overflow-hidden rounded-lg border border-border text-xs">
          <button
            onClick={() => setShowRaw(false)}
            className={`px-2.5 py-1 ${!showRaw ? "bg-elev text-fg" : "text-muted hover:text-fg"}`}
          >
            Rendered
          </button>
          <button
            onClick={() => setShowRaw(true)}
            className={`border-l border-border px-2.5 py-1 ${showRaw ? "bg-elev text-fg" : "text-muted hover:text-fg"}`}
            title="The full canonical Markdown stored on disk (frontmatter + body)"
          >
            Raw .md
          </button>
        </div>
        {showRaw && <CopyButton text={data.raw} />}
      </div>

      {showRaw ? (
        <pre className="overflow-auto whitespace-pre-wrap rounded-lg border border-border bg-elev p-4 text-xs leading-relaxed text-fg">
          {data.raw}
        </pre>
      ) : (
        <Markdown body={data.body} />
      )}

      {data.supersedes && (
        <p className="mt-6 text-xs text-muted">
          Supersedes <PageLink id={data.supersedes} />
        </p>
      )}
    </div>
  );
}
