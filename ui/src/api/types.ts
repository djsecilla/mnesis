// Typed shapes mirroring the G1 REST gateway (src/mnesis/webapi.py).

export interface PageSummary {
  id: string;
  title: string;
  kind: string;
  status: string;
  confidence: number;
  updated: string;
  tags: string[];
}

export interface PagesResponse {
  pages: PageSummary[];
  total: number;
}

export interface Relation {
  s: string;
  p: string;
  o: string;
}

export interface PageDetail {
  id: string;
  frontmatter: Record<string, unknown> & { title: string; tags: string[] };
  body: string;
  raw: string;
  confidence: number;
  breakdown: Record<string, number | boolean | string>;
  relations: Relation[];
  supersedes: string | null;
  superseded_by: string | null;
  contradicts: string[];
  open_contradiction: boolean;
}

export interface SearchHit {
  id: string;
  title: string;
  snippet: string;
  bm25_score: number;
  confidence: number;
  graph_proximity: number;
  final_score: number;
  status: string;
  grounding: unknown | null;
}

export interface SearchResponse {
  query: string;
  hits: SearchHit[];
}

export interface GraphNode {
  ref: string;
  type: string;
  degree: number;
}

export interface GraphEdge {
  s: string;
  p: string;
  o: string;
  confidence: number;
  assertion_count: number;
  demoted: boolean;
  source_pages: string[];
}

export interface GraphData {
  root: string | null;
  depth: number;
  include_demoted: boolean;
  nodes: GraphNode[];
  edges: GraphEdge[];
}

export interface EntityData {
  ref: string;
  type: string;
  pages: string[];
  edges: GraphEdge[];
}

export interface ImpactItem {
  ref: string;
  hop: number;
  predicate: string;
  path: string[];
  grounding_pages: string[];
  confidence: number;
}

export interface ImpactResponse {
  entity: string;
  affected: ImpactItem[];
}

export interface FilebackResponse {
  filed: boolean;
  digest_id: string | null;
  message?: string;
  reason?: string;
}

export interface RetrievalHit {
  id: string;
  title: string;
  kind: string;
  status: string;
  confidence: number;
  bm25_score: number;
  graph_proximity: number;
  final_score: number;
}

export interface ChatDone {
  citations: string[];
  retrieval: RetrievalHit[];
}
