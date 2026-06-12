import { apiGet, apiPost } from "./client";
import type {
  EntityData,
  FilebackResponse,
  GraphData,
  ImpactResponse,
  PageDetail,
  PagesResponse,
  SearchResponse,
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
