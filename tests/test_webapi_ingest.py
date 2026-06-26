"""Tests for the ingestion / sources / reviews REST endpoints (stub mode).

Covers: preview (JSON + multipart) writes nothing; commit writes the outcome;
oversized + unsupported uploads rejected cleanly; sources provenance without
leaking redacted values; reviews list + resolve; auth required for writes.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from mnesis import config, mcp_server, search, state, store, webapi, tenancy
from mnesis.store import Page

TOKEN = "ingest-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
SECRET = "sk-ABCDEF0123456789abcdef"
SECRET_TEXT = f"Project Atlas uses Redis for caching. Deploy key {SECRET}."


@pytest.fixture()
def client(monkeypatch):
    tmp = Path(tempfile.mkdtemp(prefix="mnesis-ingest-"))
    monkeypatch.setattr(config, "DATA_ROOT", tmp / "data")
    monkeypatch.setattr(config, "MNESIS_LLM_STUB", True)
    monkeypatch.setattr(config, "MNESIS_MCP_TOKEN", TOKEN)

    ctx = tenancy.open_tenant(config.DEFAULT_TENANT_ID)
    token = tenancy.bind(ctx)
    search.rebuild()

    app = Starlette()
    webapi.mount_api(app)
    app.add_middleware(mcp_server._BearerAuthMiddleware, token=TOKEN)
    app.add_middleware(mcp_server._TenantBindingMiddleware)  # bind a tenant per request
    with TestClient(app) as c:
        yield c
    tenancy.unbind(token)
    shutil.rmtree(tmp, ignore_errors=True)


def _commits(root: Path) -> int:
    # The tenant root IS its own git repo.
    out = subprocess.run(["git", "-C", str(root), "rev-list", "--count", "--all"],
                         capture_output=True, text=True)
    return int((out.stdout or "0").strip() or "0")


# --- preview is side-effect-free --------------------------------------------


def test_preview_text_returns_plan_and_writes_nothing(client):
    before = (_commits(tenancy.current().root_path), sorted(os.listdir(tenancy.current().sources_dir)))
    r = client.post("/api/ingest/preview", json={"text": SECRET_TEXT}, headers=AUTH)
    assert r.status_code == 200
    plan = r.json()
    assert plan["draft_page"]["title"]
    assert plan["routing"]["action"] == "new"
    assert {"type": "secret", "kind": "api-key", "count": 1} in plan["redactions"]
    assert SECRET not in r.text  # never echo the raw value back
    assert (_commits(tenancy.current().root_path), sorted(os.listdir(tenancy.current().sources_dir))) == before


def test_preview_multipart_markdown(client):
    files = {"file": ("note.md", b"Atlas uses Redis for caching.", "text/markdown")}
    r = client.post("/api/ingest/preview", files=files, data={"source_ref": "note"}, headers=AUTH)
    assert r.status_code == 200
    plan = r.json()
    assert plan["source_ref"] == "note"
    assert plan["draft_page"]["title"]
    assert os.listdir(tenancy.current().sources_dir) == []  # still nothing written


# --- commit writes ----------------------------------------------------------


def test_commit_writes_outcome(client):
    plan = client.post("/api/ingest/preview", json={"text": SECRET_TEXT}, headers=AUTH).json()
    r = client.post("/api/ingest/commit", json={"plan": plan}, headers=AUTH)
    assert r.status_code == 200
    result = r.json()
    assert result["action_taken"] == "new"
    assert result["redaction_count"] == 1
    assert store.page_exists(result["page_id"])


def test_commit_refreshes_the_graph(client):
    # A source with an entity/relation should appear in the graph right after
    # commit (the commit endpoint rebuilds the graph cache, not just the index).
    before = {n["ref"] for n in client.get("/api/graph", headers=AUTH).json()["nodes"]}
    assert "library:kafka" not in before
    text = "Zeta streams events with Kafka. rel{project:zeta|uses|library:kafka}"
    plan = client.post("/api/ingest/preview", json={"text": text}, headers=AUTH).json()
    client.post("/api/ingest/commit", json={"plan": plan}, headers=AUTH)
    after = {n["ref"] for n in client.get("/api/graph", headers=AUTH).json()["nodes"]}
    assert {"project:zeta", "library:kafka"} <= after


# --- upload validation ------------------------------------------------------


def test_oversized_upload_rejected(client, monkeypatch):
    monkeypatch.setattr(config, "MNESIS_MAX_UPLOAD_BYTES", 16)
    files = {"file": ("big.md", b"x" * 64, "text/markdown")}
    r = client.post("/api/ingest/preview", files=files, headers=AUTH)
    assert r.status_code == 413
    assert r.json()["code"] == "payload_too_large"


def test_unsupported_content_type_rejected(client):
    files = {"file": ("doc.pdf", b"%PDF-1.4 ...", "application/pdf")}
    r = client.post("/api/ingest/preview", files=files, headers=AUTH)
    assert r.status_code == 415
    assert r.json()["code"] == "unsupported_type"
    assert "PDF" in r.json()["message"]


# --- sources provenance, no leak --------------------------------------------


def test_sources_list_and_detail_without_leak(client):
    plan = client.post("/api/ingest/preview", json={"text": SECRET_TEXT}, headers=AUTH).json()
    client.post("/api/ingest/commit", json={"plan": plan}, headers=AUTH)
    ref = plan["source_ref"]

    lst = client.get("/api/sources", headers=AUTH).json()
    assert lst["total"] == 1
    entry = lst["sources"][0]
    assert entry["id"] == ref
    assert entry["ingested_at"]
    assert entry["pages"] and entry["pages"][0]["id"]

    detail = client.get(f"/api/sources/{ref}", headers=AUTH).json()
    assert SECRET not in detail["text"]      # redacted at ingest
    assert "REDACTED" in detail["text"]
    assert detail["pages"][0]["title"]


def test_source_detail_404_and_path_safety(client):
    assert client.get("/api/sources/nope", headers=AUTH).status_code == 404
    # a traversal-y id is rejected before any read
    assert client.get("/api/sources/..", headers=AUTH).status_code in (400, 404)


# --- reviews list + resolve -------------------------------------------------


def _seed_contradiction() -> int:
    """Two cross-linked contradicting pages + an open review; returns review id."""
    a = Page(id="atlas-redis", title="Atlas uses Redis", body="Redis.",
             sources=["src-a"], contradicts=["atlas-memcached"], kind="fact")
    b = Page(id="atlas-memcached", title="Atlas uses Memcached", body="Memcached.",
             sources=["src-b"], contradicts=["atlas-redis"], kind="fact")
    store.write_page(a)
    store.write_page(b)
    search.rebuild()
    return state.enqueue_contradiction("atlas-redis", "atlas-memcached", "redis vs memcached")


def test_reviews_list_and_resolve(client):
    rid = _seed_contradiction()

    listing = client.get("/api/reviews", headers=AUTH).json()
    assert listing["total"] == 1
    review = listing["reviews"][0]
    assert review["id"] == rid
    assert {review["page_a"]["id"], review["page_b"]["id"]} == {"atlas-redis", "atlas-memcached"}
    assert review["page_a"]["confidence"] is not None

    r = client.post(f"/api/reviews/{rid}/resolve",
                    json={"keep_page_id": "atlas-redis"}, headers=AUTH)
    assert r.status_code == 200
    body = r.json()
    assert body["resolved"] and body["superseded"] == "atlas-memcached"

    loser = store.read_page("atlas-memcached")
    assert loser.status == "stale"
    assert loser.superseded_by == "atlas-redis"
    assert client.get("/api/reviews", headers=AUTH).json()["total"] == 0  # queue cleared


def test_resolve_bad_keep_is_rejected(client):
    rid = _seed_contradiction()
    r = client.post(f"/api/reviews/{rid}/resolve",
                    json={"keep_page_id": "not-in-review"}, headers=AUTH)
    assert r.status_code == 400
    assert r.json()["code"] == "invalid_keep"


# --- auth -------------------------------------------------------------------


def test_writes_require_token(client):
    r = client.post("/api/ingest/preview", json={"text": "x"})  # no Authorization
    assert r.status_code == 401
