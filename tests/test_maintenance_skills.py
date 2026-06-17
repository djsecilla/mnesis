"""Tests for the dream-cycle maintenance Agent Skills.

Offline, no model. Validates that the F3 skills loader discovers all five
maintenance skills by name+description (metadata only) and activates each, that
their frontmatter is valid, and that running each skill's deterministic helper
script against the **fake Mnesis maintenance tools** yields the documented
structured output — and that the propose-only skills perform NO out-of-policy
writes (triage/dedup resolve/apply nothing).
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from mnesis_agents.knowledge import FakeMaintenanceTools, MAINTENANCE_TOOL_NAMES
from mnesis_agents.skills import loader
from mnesis_agents.skills.loader import SkillRegistry, load_skill

BUNDLED = Path(loader.__file__).resolve().parent / "bundled"

MAINTENANCE_SKILLS = {
    "decay-sweep",
    "graph-hygiene",
    "contradiction-triage",
    "deduplication",
    "quality-sweep",
}

# Skills that auto-apply vs propose-only (read-only counts as propose-nothing).
AUTO_APPLY = {"decay-sweep", "graph-hygiene"}
PROPOSE_ONLY = {"contradiction-triage", "deduplication", "quality-sweep"}

# Write tools no propose-only skill may declare in its allowed-tools.
WRITE_TOOLS = {"mnesis_resolve", "mnesis_ingest", "mnesis_file_back"}


@pytest.fixture()
def registry() -> SkillRegistry:
    return SkillRegistry(dirs=[BUNDLED]).discover()


@pytest.fixture()
def fake_tools() -> dict:
    return {t.name: t for t in asyncio.run(FakeMaintenanceTools().load_tools())}


def _run_script(skill_name: str, script_rel: str, payload: dict, tmp_path: Path) -> dict:
    """Activate a bundled skill and run its helper script over ``payload`` JSON
    (passed as a file arg, the only input channel run_script offers)."""
    infile = tmp_path / "input.json"
    infile.write_text(json.dumps(payload), encoding="utf-8")
    sk = load_skill(BUNDLED / skill_name)
    res = sk.run_script(script_rel, [str(infile)])
    assert res["returncode"] == 0, res["stderr"]
    return json.loads(res["stdout"])


# ── Level 1: discovery (metadata only) ──────────────────────────────────────


def test_loader_discovers_all_maintenance_skills(registry):
    assert registry.issues == []
    assert MAINTENANCE_SKILLS <= set(registry.names())
    # Discovery is metadata-only: each card carries a non-empty description and a
    # SKILL.md path, and no instruction body was loaded.
    for name in MAINTENANCE_SKILLS:
        card = registry.card(name)
        assert card.description.strip()
        assert (card.path / "SKILL.md").is_file()
        assert not hasattr(card, "instructions")


def test_cards_prompt_lists_maintenance_skills(registry):
    text = registry.cards_prompt()
    for name in MAINTENANCE_SKILLS:
        assert name in text


# ── Level 2: activation + frontmatter validation ────────────────────────────


@pytest.mark.parametrize("name", sorted(MAINTENANCE_SKILLS))
def test_activation_and_frontmatter_valid(registry, name):
    sk = registry.activate(name)
    assert sk.name == name
    assert sk.instructions.strip()  # full body loaded on activation
    assert "Policy" in sk.instructions  # each states its auto-vs-propose policy
    assert sk.allowed_tools  # declares the Mnesis tools it uses
    assert all(t.startswith("mnesis_") for t in sk.allowed_tools)


def test_propose_only_skills_declare_no_write_tools(registry):
    """The propose-vs-auto boundary is encoded in the manifest: a proposal-only
    skill never lists a write tool, so it cannot resolve/apply even if asked."""
    for name in PROPOSE_ONLY:
        allowed = set(registry.activate(name).allowed_tools)
        assert not (allowed & WRITE_TOOLS), f"{name} must not declare write tools, got {allowed}"


def test_auto_apply_skills_declare_their_writer(registry):
    assert "mnesis_decay" in registry.activate("decay-sweep").allowed_tools
    assert "mnesis_graph_lint" in registry.activate("graph-hygiene").allowed_tools


# ── Procedure execution against the fake Mnesis tools ───────────────────────


def test_decay_sweep_procedure(fake_tools, tmp_path):
    decay = json.loads(fake_tools["mnesis_decay"].invoke({}))
    out = _run_script("decay-sweep", "scripts/summarize.py", decay, tmp_path)
    assert out["skill"] == "decay-sweep"
    assert out["action"] == "auto_applied" and out["auto_apply"] is True
    assert out["summary"] == {"scanned": 6, "restaled": 1, "reactivated": 0, "unchanged": 5}
    assert "stale" in out["message"]


def test_graph_hygiene_procedure(fake_tools, tmp_path):
    report = json.loads(fake_tools["mnesis_graph_lint"].invoke({"fix": False}))
    applied = json.loads(fake_tools["mnesis_graph_lint"].invoke({"fix": True}))
    out = _run_script(
        "graph-hygiene", "scripts/summarize.py", {"report": report, "applied": applied}, tmp_path
    )
    assert out["action"] == "auto_applied" and out["auto_apply"] is True
    assert out["fixed"]["total"] == 2  # 1 duplicate + 1 confidence update
    # The undeclared entity is flagged for a human, never auto-fixed.
    assert any(f["category"] == "undeclared_entities" for f in out["flagged_for_human"])


def test_contradiction_triage_proposes_only(fake_tools, tmp_path):
    review = json.loads(fake_tools["mnesis_review"].invoke({}))
    payload = {"contradictions": review["open"]}
    out = _run_script("contradiction-triage", "scripts/triage.py", payload, tmp_path)
    assert out["action"] == "propose" and out["auto_apply"] is False
    assert len(out["proposals"]) == 1
    prop = out["proposals"][0]
    # Higher confidence + more sources + more recent → keep atlas-redis.
    assert prop["keep"] == "atlas-redis" and prop["supersede"] == "atlas-memcached"
    assert prop["strength"] == "strong"
    # Out-of-policy writes: nothing was resolved; the output is proposals only.
    blob = json.dumps(out)
    assert '"resolved"' not in blob and '"kept"' not in blob
    assert "PROPOSALS ONLY" in out["note"]


def test_deduplication_proposes_only(fake_tools, tmp_path):
    dupes = json.loads(fake_tools["mnesis_find_duplicates"].invoke({}))
    payload = {
        "candidates": dupes["candidates"],
        "strong_threshold": 0.5,
        "pages": {"atlas-redis": {"confidence": 0.82}, "atlas-redis-cache": {"confidence": 0.50}},
    }
    out = _run_script("deduplication", "scripts/propose.py", payload, tmp_path)
    assert out["action"] == "propose" and out["auto_apply"] is False
    assert len(out["proposals"]) == 1  # only the strong pair (0.62)
    prop = out["proposals"][0]
    assert {prop["page_a"], prop["page_b"]} == {"atlas-redis", "atlas-redis-cache"}
    assert prop["proposed_action"] == "supersede"
    assert prop["keep"] == "atlas-redis"  # higher confidence
    # The weak pair (0.28) is skipped, not applied.
    assert {s["page_a"] for s in out["skipped_weak"]} == {"pg-backups"}
    assert '"merged"' not in json.dumps(out)


def test_quality_sweep_is_read_only_findings(fake_tools, tmp_path):
    health = json.loads(fake_tools["mnesis_health_report"].invoke({}))
    out = _run_script("quality-sweep", "scripts/findings.py", health, tmp_path)
    assert out["action"] == "report" and out["auto_apply"] is False
    types = {f["type"] for f in out["findings"]}
    assert "no_source_pages" in types and "low_confidence" in types
    assert "open_contradictions" in types
    # High-severity findings sort first.
    assert out["findings"][0]["severity"] == "high"
    assert out["summary"]["pages_total"] == 7


def test_quality_sweep_flags_stale_caches(tmp_path):
    health = {
        "pages_total": 3, "by_status": {"active": 3}, "by_kind": {"fact": 3},
        "no_sources": [], "low_confidence": 0, "low_confidence_pages": [],
        "stale": 0, "open_contradictions": 0,
        "graph": {"entities": 1, "edges": 0, "demoted": 0},
        "orphan_entities": 0, "undeclared_entities": 0, "dangling_structural": 0,
        "index": {"fresh": False, "missing_from_index": ["late"]},
        "graph_index": {"present": True, "fresh": False, "missing_page_nodes": ["late"]},
    }
    out = _run_script("quality-sweep", "scripts/findings.py", health, tmp_path)
    types = {f["type"] for f in out["findings"]}
    assert "index_stale" in types and "graph_cache_stale" in types


# ── Fake maintenance source sanity (the write tool exists but is never used) ──


def test_fake_maintenance_source_exposes_expected_tools(fake_tools):
    assert set(fake_tools) == set(MAINTENANCE_TOOL_NAMES)
    # The write tool is available in the source — the skills' policy (not the
    # absence of the tool) is what keeps triage/dedup from calling it.
    assert "mnesis_resolve" in fake_tools
