import { apiGet, apiPost, API_BASE, authHeaders } from "./client";
import type {
  EntityData,
  FilebackResponse,
  GraphData,
  ImpactResponse,
  IngestOverrides,
  IngestPlan,
  IngestResult,
  PageDetail,
  PagesResponse,
  ResolveResponse,
  ReviewsResponse,
  SearchResponse,
  SourceDetail,
  SourcesResponse,
} from "./types";

function qs(params: object): string {
  const parts = Object.entries(params)
    .filter(([, v]) => v !== undefined && v !== null && v !== "")
    .map(([k, v]) => `${encodeURIComponent(k)}=${encodeURIComponent(String(v))}`);
  return parts.length ? `?${parts.join("&")}` : "";
}

export interface PageQuery {
  status?: string;
  kind?: string;
  q?: string;
}

export const listPages = (params: PageQuery = {}) =>
  apiGet<PagesResponse>(`/pages${qs(params)}`);

export const getPage = (id: string) => apiGet<PageDetail>(`/pages/${encodeURIComponent(id)}`);

export const search = (q: string, limit = 10) =>
  apiGet<SearchResponse>(`/search${qs({ q, limit })}`);

export interface GraphQuery {
  root?: string;
  depth?: number;
  include_demoted?: boolean;
}

export const getGraph = (params: GraphQuery = {}) => apiGet<GraphData>(`/graph${qs(params)}`);

export const getEntity = (ref: string) => apiGet<EntityData>(`/entity/${encodeURIComponent(ref)}`);

export const getImpact = (ref: string, depth = 3) =>
  apiGet<ImpactResponse>(`/impact/${encodeURIComponent(ref)}${qs({ depth })}`);

export const fileback = (question: string, answer: string) =>
  apiPost<FilebackResponse>(`/fileback`, { question, answer });

// --- Ingestion: preview (side-effect-free) + commit ------------------------

/** Surface the gateway's structured {code, message} errors as the thrown message. */
async function unwrap<T>(res: Response): Promise<T> {
  const body = await res.text();
  // Parse defensively: a non-JSON error body (e.g. a plain-text 500) must yield
  // a clean message, never an "Unexpected token" JSON.parse crash.
  let data: unknown = null;
  try {
    data = body ? JSON.parse(body) : null;
  } catch {
    if (!res.ok) {
      throw new Error(`${res.status} ${res.statusText}${body ? ` — ${body.slice(0, 200)}` : ""}`);
    }
    throw new Error("Unexpected non-JSON response from the server.");
  }
  if (!res.ok) {
    const d = data as { message?: string; error?: string } | null;
    throw new Error(d?.message || d?.error || `${res.status} ${res.statusText}`);
  }
  return data as T;
}

export interface PreviewInput {
  text?: string;
  file?: File;
  sourceRef?: string;
}

/** POST /api/ingest/preview — JSON for pasted text, multipart for a file. */
export async function ingestPreview(input: PreviewInput): Promise<IngestPlan> {
  if (input.file) {
    const form = new FormData();
    form.append("file", input.file);
    if (input.sourceRef) form.append("source_ref", input.sourceRef);
    return unwrap(
      await fetch(`${API_BASE}/ingest/preview`, { method: "POST", headers: authHeaders(), body: form }),
    );
  }
  return unwrap(
    await fetch(`${API_BASE}/ingest/preview`, {
      method: "POST",
      headers: authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({ text: input.text ?? "", source_ref: input.sourceRef }),
    }),
  );
}

/** POST /api/ingest/commit — apply a previewed plan with curation overrides. */
export async function ingestCommit(plan: IngestPlan, overrides?: IngestOverrides): Promise<IngestResult> {
  return unwrap(
    await fetch(`${API_BASE}/ingest/commit`, {
      method: "POST",
      headers: authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({ plan, overrides }),
    }),
  );
}

// --- Sources ----------------------------------------------------------------

export const listSources = () => apiGet<SourcesResponse>(`/sources`);

export const getSource = (id: string) => apiGet<SourceDetail>(`/sources/${encodeURIComponent(id)}`);

// --- Reviews (contradiction queue) -----------------------------------------

export const listReviews = () => apiGet<ReviewsResponse>(`/reviews`);

export const resolveReview = (id: number, keepPageId: string) =>
  apiPost<ResolveResponse>(`/reviews/${id}/resolve`, { keep_page_id: keepPageId });
