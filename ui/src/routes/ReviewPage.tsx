import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link } from "react-router-dom";
import { getPage, listReviews, resolveReview } from "../api/endpoints";
import type { Review, ReviewPageRef } from "../api/types";
import { ConfidenceBar } from "../components/Confidence";
import { Spinner } from "../components/IngestReview";

export default function ReviewPage() {
  const { data, isLoading, error } = useQuery({ queryKey: ["reviews"], queryFn: listReviews });
  const reviews = data?.reviews ?? [];

  return (
    <div className="mx-auto max-w-3xl p-8">
      <header className="mb-4">
        <h1 className="text-xl font-semibold">Review contradictions</h1>
        <p className="mt-1 text-sm text-muted">
          When two pages conflict with no clear winner, both are kept and queued here. Keep one — the
          other is superseded (marked stale) and the kept page’s confidence recovers.
        </p>
      </header>

      {isLoading && <p className="text-muted">Loading…</p>}
      {error && <p className="text-muted">Could not load reviews — is the API running?</p>}
      {data && reviews.length === 0 && (
        <div className="rounded-lg border border-dashed border-border px-4 py-12 text-center text-muted">
          No open contradictions.
        </div>
      )}

      <div className="space-y-6">
        {reviews.map((r) => (
          <ReviewCard key={r.id} review={r} />
        ))}
      </div>
    </div>
  );
}

function ReviewCard({ review }: { review: Review }) {
  const qc = useQueryClient();
  const [pendingKeep, setPendingKeep] = useState<string | null>(null);

  const resolve = useMutation({
    mutationFn: (keepId: string) => resolveReview(review.id, keepId),
    onSuccess: () => {
      // The queue, the page list, the graph, and both pages' cached state are stale.
      qc.invalidateQueries({ queryKey: ["reviews"] });
      qc.invalidateQueries({ queryKey: ["pages"] });
      qc.invalidateQueries({ queryKey: ["graph"] });
      qc.invalidateQueries({ queryKey: ["palette-graph"] });
      qc.invalidateQueries({ queryKey: ["page", review.page_a.id] });
      qc.invalidateQueries({ queryKey: ["page", review.page_b.id] });
    },
  });

  const keep = pendingKeep === review.page_a.id ? review.page_a : pendingKeep === review.page_b.id ? review.page_b : null;
  const stale = keep ? (keep.id === review.page_a.id ? review.page_b : review.page_a) : null;

  return (
    <div className="card space-y-3 p-4">
      <div className="text-xs text-muted">
        Conflict #{review.id}
        {review.detail ? <> — {review.detail}</> : null}
      </div>

      <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
        <PageColumn page={review.page_a} onKeep={() => setPendingKeep(review.page_a.id)} disabled={resolve.isPending} />
        <PageColumn page={review.page_b} onKeep={() => setPendingKeep(review.page_b.id)} disabled={resolve.isPending} />
      </div>

      {keep && stale && (
        <div className="space-y-2 rounded-lg border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-sm">
          <div>
            Keep <span className="font-medium">{keep.title ?? keep.id}</span>?{" "}
            <span className="font-medium">{stale.title ?? stale.id}</span> will be marked stale.
          </div>
          <div className="flex items-center gap-2">
            <button
              onClick={() => resolve.mutate(keep.id)}
              disabled={resolve.isPending}
              className="inline-flex items-center gap-2 rounded-lg bg-accent px-3 py-1.5 text-xs font-medium text-accent-fg disabled:opacity-50"
            >
              {resolve.isPending && <Spinner />}
              {resolve.isPending ? "Resolving…" : "Confirm — keep this one"}
            </button>
            <button
              onClick={() => setPendingKeep(null)}
              disabled={resolve.isPending}
              className="rounded-lg border border-border px-3 py-1.5 text-xs hover:border-accent disabled:opacity-50"
            >
              Cancel
            </button>
          </div>
        </div>
      )}

      {resolve.isError && <p className="text-sm text-red-400">{(resolve.error as Error).message}</p>}
    </div>
  );
}

function PageColumn({ page, onKeep, disabled }: { page: ReviewPageRef; onKeep: () => void; disabled: boolean }) {
  const { data } = useQuery({ queryKey: ["page", page.id], queryFn: () => getPage(page.id), staleTime: 10_000 });
  const snippet = data?.body
    ?.split("\n")
    .map((l) => l.trim())
    .find((l) => l.length > 0)
    ?.slice(0, 180);

  return (
    <div className="flex flex-col gap-2 rounded-lg border border-border p-3">
      <Link to={`/pages/${encodeURIComponent(page.id)}`} className="text-sm font-medium hover:text-accent hover:underline">
        {page.title ?? page.id}
      </Link>
      <span className="block truncate text-xs text-muted">{page.id}</span>
      {page.confidence != null && <ConfidenceBar value={page.confidence} />}
      {snippet && <p className="text-xs leading-relaxed text-muted">{snippet}</p>}
      <button
        onClick={onKeep}
        disabled={disabled}
        className="mt-auto rounded-lg border border-border px-3 py-1.5 text-xs hover:border-accent disabled:opacity-50"
      >
        Keep this one
      </button>
    </div>
  );
}
