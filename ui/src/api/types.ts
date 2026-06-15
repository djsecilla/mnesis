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
  mentions: number; // distinct pages referencing the entity (drives node size)
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

export interface EntitySource {
  id: string;
  title: string;
  kind: string;
  confidence: number;
  snippet: string;
}

export interface RelatedEntity {
  ref: string;
  type: string;
  predicate: string;
  direction: string; // "out" (ref -> entity) | "in" (entity -> ref)
  confidence: number;
}

export interface EntityData {
  ref: string;
  type: string;
  confidence?: number | null;
  summary: string;
  sources: EntitySource[];
  tags: string[];
  related: RelatedEntity[];
  pages: string[]; // back-compat (edge source pages)
  edges: GraphEdge[]; // back-compat (page reader reads edge confidence)
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

// --- Ingestion (plan / apply) ----------------------------------------------

export interface Redaction {
  type: string;
  kind: string;
  count: number;
}

export interface DraftPage {
  title: string;
  summary_markdown: string;
  body: string;
  tags: string[];
  relations: Relation[];
  kind: string;
}

export interface RoutingCandidate {
  page_id: string;
  title: string;
  relation_label: string;
  confidence: number;
}

export type RoutingAction = "new" | "reinforce" | "supersede" | "contradict";

export interface Routing {
  action: RoutingAction;
  target_page_id: string | null;
  candidates: RoutingCandidate[];
  auto_resolved: boolean;
  margin: number | null;
}

export interface IngestPlan {
  source_ref: string;
  redacted_text: string;
  redactions: Redaction[];
  draft_page: DraftPage;
  routing: Routing;
  warnings: string[];
}

export interface IngestOverrides {
  title?: string;
  tags?: string[];
  accepted_relations?: number[];
  rejected_relations?: number[];
  routing?: { action: string; target_page_id?: string | null };
}

export interface IngestResult {
  action_taken: string;
  page_id: string;
  superseded_id: string | null;
  review_id: number | null;
  redaction_count: number;
}

// --- Sources (provenance) --------------------------------------------------

export interface SourcePageRef {
  id: string;
  title: string;
}

export interface SourceSummary {
  id: string;
  ingested_at: string | null;
  pages: SourcePageRef[];
}

export interface SourcesResponse {
  sources: SourceSummary[];
  total: number;
}

export interface SourceDetail {
  id: string;
  ingested_at: string | null;
  text: string;
  pages: SourcePageRef[];
}

// --- Reviews (contradiction queue) -----------------------------------------

export interface ReviewPageRef {
  id: string;
  title: string | null;
  confidence: number | null;
}

export interface Review {
  id: number;
  page_a: ReviewPageRef;
  page_b: ReviewPageRef;
  detail: string;
}

export interface ReviewsResponse {
  reviews: Review[];
  total: number;
}

export interface ResolveResponse {
  resolved: boolean;
  review_id: number;
  kept: string;
  superseded: string | null;
  message: string;
}
