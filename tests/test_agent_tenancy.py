"""T6 — multitenant agent layer (stub).

Each agent runs confined to one tenant: it reaches Mnesis only through that
tenant's MCP credential (so via T3/T5 it can only touch that tenant's store), and
ALL of its agent-side governance state — run audit, dream proposals/reports, the
writing processed/dead-letter ledgers, action proposals, and the egress
allowlist/quotas/send-audit — lives under that tenant's own directories. A
tenant-A agent cannot read B's data, use B's egress config, or write to B's audit.
Resolution is fail-closed: an agent with no resolvable tenant credential won't start.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from mnesis_agents import cli, tenancy
from mnesis_agents.egress import Recipient
from mnesis_agents.knowledge import FakeMaintenanceTools, FakeMnesisTools, ToolRegistry


def _fakes():
    return asyncio.run(ToolRegistry([FakeMaintenanceTools(), FakeMnesisTools()]).get_tools())


@pytest.fixture()
def scopes(tmp_path):
    """Two fully-isolated tenant scopes under one state base, with distinct
    credentials and distinct egress allowlists."""
    a = tenancy.resolve_scope(
        "alpha", "tok-alpha", state_base=tmp_path,
        egress=tenancy.EgressSettings(
            enabled=True, recipient_allowlist=frozenset({"ann@alpha.com"}),
            endpoint_allowlist=frozenset({"smtp.alpha:587"}),
        ),
    )
    b = tenancy.resolve_scope(
        "beta", "tok-beta", state_base=tmp_path,
        egress=tenancy.EgressSettings(
            enabled=True, recipient_allowlist=frozenset({"bob@beta.com"}),
            endpoint_allowlist=frozenset({"smtp.beta:587"}),
        ),
    )
    return a, b


# ── credential confinement: an agent reaches only its tenant's Mnesis ───────


def test_agent_tools_carry_only_their_tenants_credential(scopes):
    a, b = scopes
    conn_a = a.knowledge_source()._connections["mnesis"]
    conn_b = b.knowledge_source()._connections["mnesis"]
    assert conn_a["headers"]["Authorization"] == "Bearer tok-alpha"
    assert conn_b["headers"]["Authorization"] == "Bearer tok-beta"
    # A never carries B's token (so server-side T3/T5 confines it to alpha).
    assert conn_a["headers"] != conn_b["headers"]


# ── governance-state isolation: every store path is the tenant's own ────────


def test_every_governance_path_is_partitioned_per_tenant(scopes):
    a, b = scopes
    assert a.state_root != b.state_root
    assert "alpha" in str(a.state_root) and "beta" in str(b.state_root)
    for attr in ("runs_dir", "audit_dir", "proposals_dir", "connector_state_dir",
                 "dead_letter_dir", "egress_state_dir", "send_audit_file",
                 "egress_ledger", "checkpoint_db", "notes_inbox", "action_outbox"):
        assert getattr(a, attr) != getattr(b, attr)
        # A's path is never inside B's root and vice-versa.
        assert not str(getattr(a, attr)).startswith(str(b.state_root))


# ── a maintenance cycle for A curates only A (writes only A's stores) ───────


def test_dream_cycle_for_a_writes_only_as_stores(scopes):
    a, b = scopes
    agent_a = cli.build_tenant_dream_agent(a, tools=_fakes())
    report = agent_a.run_and_record()
    assert report.totals is not None  # a real cycle ran

    # A's report landed under A's state; B's stores are untouched (no dirs/files).
    assert (a.proposals_dir / "dream-cycles.jsonl").exists()
    assert not b.proposals_dir.exists() or not any(b.proposals_dir.iterdir())
    assert not b.state_root.exists() or not any(
        p for p in b.state_root.rglob("*") if p.is_file()
    )


def test_two_tenants_dream_cycles_do_not_cross(scopes):
    a, b = scopes
    cli.build_tenant_dream_agent(a, tools=_fakes()).run_and_record()
    cli.build_tenant_dream_agent(b, tools=_fakes()).run_and_record()
    a_reports = (a.proposals_dir / "dream-cycles.jsonl").read_text(encoding="utf-8")
    b_reports = (b.proposals_dir / "dream-cycles.jsonl").read_text(encoding="utf-8")
    # Each tenant's report log is its own file (separate paths, both populated).
    assert a.proposals_dir != b.proposals_dir
    assert a_reports and b_reports


# ── egress: A cannot use B's allowlist; ledgers are separate ────────────────


def test_egress_config_and_ledgers_are_per_tenant(scopes):
    a, b = scopes
    pol_a, pol_b = a.egress_policy(), b.egress_policy()

    # A's policy refuses a recipient that is only on B's allowlist — A cannot use
    # B's egress config.
    assert pol_a.validate_recipient(Recipient("bob@beta.com", "policy")).denied
    assert pol_b.validate_recipient(Recipient("bob@beta.com", "policy")).allowed
    assert pol_a.validate_recipient(Recipient("ann@alpha.com", "policy")).allowed
    assert pol_b.validate_recipient(Recipient("ann@alpha.com", "policy")).denied
    # Quota ledgers + send-audit are separate physical files.
    assert pol_a.quota_store.path != pol_b.quota_store.path
    assert a.send_audit().path != b.send_audit().path


def test_send_audit_is_per_tenant(scopes):
    a, b = scopes
    a.send_audit().record(
        proposal_id="p1", approval_id="ap1", channel="email",
        recipient="ann@alpha.com", endpoint="smtp.alpha:587",
        content_hash="sha256:x", decision="allow", status="sent",
    )
    assert a.send_audit().all() and a.send_audit().verify()[0] is True
    # B's send-audit never saw A's record.
    assert b.send_audit().all() == []


# ── fail-closed resolution ──────────────────────────────────────────────────


def test_resolve_scope_is_fail_closed(tmp_path):
    for tid, cred in (("acme", None), ("acme", ""), (None, "tok"), ("", "tok")):
        with pytest.raises(tenancy.UnresolvedTenant):
            tenancy.resolve_scope(tid, cred, state_base=tmp_path)


def test_load_scopes_rejects_a_tenant_without_a_credential(tmp_path, monkeypatch):
    bad = tmp_path / "tenants.json"
    bad.write_text(json.dumps({"tenants": [{"tenant_id": "acme"}]}), encoding="utf-8")
    monkeypatch.setenv("MNESIS_AGENTS_TENANTS_FILE", str(bad))
    with pytest.raises(tenancy.UnresolvedTenant):
        tenancy.load_scopes()


def test_load_scopes_resolves_a_valid_tenants_file(tmp_path, monkeypatch):
    good = tmp_path / "tenants.json"
    good.write_text(json.dumps({"tenants": [
        {"tenant_id": "alpha", "credential": "tok-a"},
        {"tenant_id": "beta", "credential": "tok-b",
         "egress": {"enabled": True, "recipient_allowlist": ["bob@beta.com"]}},
    ]}), encoding="utf-8")
    monkeypatch.setenv("MNESIS_AGENTS_TENANTS_FILE", str(good))
    scopes = tenancy.load_scopes()
    assert [s.tenant_id for s in scopes] == ["alpha", "beta"]
    assert scopes[0].credential == "tok-a" and scopes[1].credential == "tok-b"
    assert "bob@beta.com" in scopes[1].egress.recipient_allowlist


# ── the runner hosts per-tenant instances, fail-closed ──────────────────────


def test_multitenant_runner_registers_isolated_per_tenant_agents(tmp_path, monkeypatch):
    from mnesis_agents import config

    monkeypatch.setattr(config, "MNESIS_AGENTS_DREAM_ENABLED", True)
    monkeypatch.setattr(config, "MNESIS_NOTES_ENABLED", False)
    monkeypatch.setattr(config, "MNESIS_AGENTS_ACTIONS_SCHEDULE_ENABLED", False)
    # Inject fake tools so no network is needed for either tenant.
    monkeypatch.setattr(cli, "_scope_tools", lambda scope: _fakes())

    tenants = tmp_path / "tenants.json"
    tenants.write_text(json.dumps({"tenants": [
        {"tenant_id": "alpha", "credential": "tok-a"},
        {"tenant_id": "beta", "credential": "tok-b"},
    ]}), encoding="utf-8")
    monkeypatch.setenv("MNESIS_AGENTS_TENANTS_FILE", str(tenants))
    monkeypatch.setenv("MNESIS_AGENTS_STATE_BASE", str(tmp_path / "state"))

    runner = cli._build_runner()  # multitenant path
    # One dream-cycle subscription per tenant.
    names = [s.name for s in runner.registry.schedule_subs]
    assert len(names) == 2


def test_runner_is_idle_when_no_tenant_resolves(tmp_path, monkeypatch):
    from mnesis_agents import config

    empty = tmp_path / "tenants.json"
    empty.write_text(json.dumps({"tenants": []}), encoding="utf-8")
    monkeypatch.setenv("MNESIS_AGENTS_TENANTS_FILE", str(empty))
    runner = cli._build_runner()
    assert runner.registry.is_empty  # no resolvable tenant → nothing starts
