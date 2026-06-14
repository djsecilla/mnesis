"""Tests for the REST + SSE web gateway (stub mode, via Starlette TestClient)."""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from mnesis import config, graph, mcp_server, search, store, webapi
from mnesis.store import Page

TOKEN = "web-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture(scope="module")
def client():
    tmp = Path(tempfile.mkdtemp(prefix="mnesis-web-"))
    saved = {k: getattr(config, k) for k in (
        "MNESIS_ROOT", "PAGES_DIR", "SOURCES_DIR", "INDEX_DIR", "MNESIS_LLM_STUB", "MNESIS_MCP_TOKEN",
    )}
    root = tmp / "wiki"
    (root / "pages").mkdir(parents=True)
    (root / "sources").mkdir(parents=True)
    config.MNESIS_ROOT = root
    config.PAGES_DIR = root / "pages"
    config.SOURCES_DIR = root / "sources"
    config.INDEX_DIR = root / ".index"
    config.MNESIS_LLM_STUB = True
    config.MNESIS_MCP_TOKEN = TOKEN

    subprocess.run(["git", "-C", str(tmp), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(tmp), "config", "user.name", "Test"], check=True)
    subprocess.run(["git", "-C", str(tmp), "config", "user.email", "t@localhost"], check=True)

    store.write_page(Page(
        id="atlas", title="Atlas uses Redis for caching", body="Project Atlas uses Redis.",
        tags=["project:atlas", "library:redis"],
        relations=[{"s": "project:atlas", "p": "uses", "o": "library:redis"}],
    ))
    store.write_page(Page(
        id="auth", title="Auth migration depends on Redis", body="The auth migration uses the cache.",
        tags=["decision:auth-migration", "library:redis"],
        relations=[{"s": "decision:auth-migration", "p": "depends_on", "o": "library:redis"}],
    ))
    # A stale page -> its edge is demoted in the graph.
    store.write_page(Page(
        id="legacy", title="Legacy redis usage", body="Old redis note.", status="stale",
        tags=["concept:legacy", "library:redis"],
        relations=[{"s": "concept:legacy", "p": "depends_on", "o": "library:redis"}],
    ))
    search.rebuild()
    graph.rebuild_graph()

    # Standalone app: the REAL /api routes (via mount_api) + the REAL auth
    # middleware + an open /health — without the FastMCP streamable app, whose
    # session manager can only be built once per process (see test_mcp_http).
    async def _health(_req):
        return JSONResponse(mcp_server._health_payload())

    app = Starlette(routes=[Route("/health", _health, methods=["GET"])])
    webapi.mount_api(app)
    app.add_middleware(mcp_server._BearerAuthMiddleware, token=TOKEN)

    with TestClient(app) as c:
        yield c

    for k, v in saved.items():
        setattr(config, k, v)
    shutil.rmtree(tmp, ignore_errors=True)


def _sse_done(text: str) -> dict:
    """Extract the final SSE `done` event's JSON payload from a streamed body."""
    payload = None
    for line in text.splitlines():
        if line.startswith("data:") and "citations" in line:
            payload = json.loads(line[len("data:"):].strip())
    assert payload is not None, f"no done event in:\n{text}"
    return payload


def _sse_answer(text: str) -> str:
    """Reconstruct the streamed answer text from the token events."""
    out = []
    in_token = False
    for line in text.splitlines():
        if line.strip() == "event: token":
            in_token = True
        elif in_token and line.startswith("data:"):
            out.append(line[len("data:"):].lstrip())
            in_token = False
    return "".join(out)


# --- auth -------------------------------------------------------------------


def test_health_open_but_api_requires_token(client):
    assert client.get("/health").status_code == 200            # open
    assert client.get("/api/pages").status_code == 401         # token required
    assert client.get("/api/pages", headers=AUTH).status_code == 200


# --- pages ------------------------------------------------------------------


def test_pages_list_shape_and_filters(client):
    data = client.get("/api/pages", headers=AUTH).json()
    assert data["total"] >= 3
    summary = next(p for p in data["pages"] if p["id"] == "atlas")
    assert set(summary) == {"id", "title", "kind", "status", "confidence", "updated", "tags"}
    assert isinstance(summary["confidence"], float)

    # status filter + q filter.
    assert all(p["status"] == "stale" for p in client.get("/api/pages?status=stale", headers=AUTH).json()["pages"])
    q = client.get("/api/pages?q=atlas", headers=AUTH).json()["pages"]
    assert [p["id"] for p in q] == ["atlas"]


def test_page_detail_shape(client):
    d = client.get("/api/pages/atlas", headers=AUTH).json()
    assert d["id"] == "atlas"
    assert d["frontmatter"]["title"] == "Atlas uses Redis for caching"
    assert d["body"].startswith("Project Atlas")
    assert d["raw"].startswith("---")  # full markdown incl. frontmatter
    assert 0.0 < d["confidence"] <= 1.0
    assert "support" in d["breakdown"] and "retention" in d["breakdown"]
    assert d["relations"] == [{"s": "project:atlas", "p": "uses", "o": "library:redis"}]
    assert d["supersedes"] is None and d["open_contradiction"] is False
    assert client.get("/api/pages/does-not-exist", headers=AUTH).status_code == 404


# --- search -----------------------------------------------------------------


def test_search_returns_component_scores(client):
    hits = client.get("/api/search?q=redis&limit=5", headers=AUTH).json()["hits"]
    assert hits
    h = hits[0]
    assert set(h) >= {"id", "title", "snippet", "bm25_score", "confidence", "graph_proximity", "final_score", "status"}


# --- graph ------------------------------------------------------------------


def test_graph_subgraph_respects_include_demoted(client):
    base = client.get("/api/graph?root=library:redis&depth=1", headers=AUTH).json()
    assert {n["ref"] for n in base["nodes"]} >= {"library:redis", "project:atlas", "decision:auth-migration"}
    # The legacy edge is demoted -> excluded by default.
    triples = {(e["s"], e["p"], e["o"]) for e in base["edges"]}
    assert ("concept:legacy", "depends_on", "library:redis") not in triples
    assert all(e["demoted"] is False for e in base["edges"])
    # nodes carry degree.
    redis = next(n for n in base["nodes"] if n["ref"] == "library:redis")
    assert redis["type"] == "library" and redis["degree"] >= 2

    incl = client.get("/api/graph?root=library:redis&depth=1&include_demoted=true", headers=AUTH).json()
    incl_triples = {(e["s"], e["p"], e["o"]) for e in incl["edges"]}
    assert ("concept:legacy", "depends_on", "library:redis") in incl_triples

    # Overview (no root) returns a bounded subgraph.
    overview = client.get("/api/graph", headers=AUTH).json()
    assert overview["root"] is None and overview["nodes"]


def test_graph_overview_includes_isolated_entities(client):
    # A page with entity tags but NO relations contributes only isolated nodes
    # (0 edges). The overview must still surface them, so a knowledge base of
    # entities-without-relations doesn't render a misleading empty graph.
    store.write_page(Page(
        id="iso-note", title="Isolated note about kafka", body="A standalone note.",
        tags=["library:kafka", "concept:streaming"], relations=[],
    ))
    graph.rebuild_graph()
    overview = client.get("/api/graph", headers=AUTH).json()
    refs = {n["ref"] for n in overview["nodes"]}
    assert "library:kafka" in refs        # isolated entity appears…
    degrees = {n["ref"]: n["degree"] for n in overview["nodes"]}
    assert degrees["library:kafka"] == 0  # …with no edges


def test_entity_and_impact(client):
    ent = client.get("/api/entity/library:redis", headers=AUTH).json()
    assert ent["type"] == "library" and ent["edges"]
    imp = client.get("/api/impact/library:redis?depth=2", headers=AUTH).json()
    refs = {a["ref"] for a in imp["affected"]}
    assert "decision:auth-migration" in refs  # depends_on redis


def test_entity_panel_payload_is_enriched(client):
    # One call returns everything the floating panel needs.
    ent = client.get("/api/entity/library:redis", headers=AUTH).json()
    assert set(ent) >= {"ref", "type", "confidence", "summary", "sources", "tags", "related"}
    # Provenance: declaring pages ranked, each with kind + confidence + snippet.
    assert ent["sources"], "expected declaring pages"
    src = ent["sources"][0]
    assert set(src) >= {"id", "title", "kind", "confidence", "snippet"}
    assert all(s["id"] in {p.id for p in store.list_pages()} for s in ent["sources"])
    # Co-occurring entity tags (type:value), redis excluded; bounded.
    assert "library:redis" not in ent["tags"]
    assert "decision:auth-migration" in ent["tags"]  # co-occurs on the auth page
    assert len(ent["tags"]) <= 8 and all(":" in t for t in ent["tags"])
    # Related = typed-edge neighbours with predicate + direction, demoted excluded.
    assert ent["related"] and all(
        {"ref", "type", "predicate", "direction", "confidence"} <= set(r) for r in ent["related"]
    )


# --- chat (SSE) -------------------------------------------------------------


def test_chat_streams_and_cites_existing_pages(client):
    r = client.post("/api/chat", json={"message": "redis", "history": []}, headers=AUTH)
    assert r.status_code == 200
    assert "event: token" in r.text
    done = _sse_done(r.text)
    assert done["citations"], "expected citations"
    existing = {p.id for p in store.list_pages()}
    assert set(done["citations"]) <= existing            # cite only pages that exist
    assert done["retrieval"]                              # hits used reported
    hit = done["retrieval"][0]                            # grounding carries component scores
    assert set(hit) >= {"id", "title", "kind", "status", "confidence", "bm25_score", "graph_proximity", "final_score"}


def test_chat_says_nothing_when_no_pages(client):
    r = client.post("/api/chat", json={"message": "zzqqxx-nonexistent"}, headers=AUTH)
    done = _sse_done(r.text)
    assert done["citations"] == [] and done["retrieval"] == []
    assert "does not contain" in _sse_answer(r.text)


def test_chat_degrades_gracefully_when_llm_unavailable(client, monkeypatch):
    # When the answer model fails (e.g. no API credits), the stream must not crash:
    # it reports llm_unavailable and still surfaces the retrieved pages.
    def boom(*_a, **_k):
        raise RuntimeError("credit balance too low")

    monkeypatch.setattr(webapi, "_grounded_answer", boom)
    r = client.post("/api/chat", json={"message": "redis", "history": []}, headers=AUTH)
    assert r.status_code == 200
    done = _sse_done(r.text)
    assert done["error"] == "llm_unavailable"
    assert done["citations"] == []
    assert done["retrieval"], "retrieved pages still reported so the user sees grounding"


# --- fileback ---------------------------------------------------------------


def test_fileback_above_and_below_threshold(client):
    long_answer = (
        "Atlas uses Redis as its primary caching layer, and the auth migration "
        "workstream depends on that same Redis cache, which Sarah owns and maintains."
    )
    above = client.post("/api/fileback", json={"question": "What caches Atlas?", "answer": long_answer}, headers=AUTH).json()
    assert above["filed"] is True and above["digest_id"]

    below = client.post("/api/fileback", json={"question": "Q?", "answer": "Too short."}, headers=AUTH).json()
    assert below["filed"] is False and below["digest_id"] is None and "below threshold" in below["reason"]
