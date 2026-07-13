"""V2 — vault resolution & access control, the security core (CLAUDE.md §16 Vaults).

The active vault is SELECTED by the client but always AUTHORIZED server-side against the
principal's grants, with the tenant taken only from the credential. Fail closed: an
ungranted, unknown, cross-tenant, malformed, or unauthenticated selection denies — and no
store is opened before authorization succeeds. This is the one place vaults differ from
tenants (never selectable), and the one place a naive implementation leaks.
"""

from __future__ import annotations

import pytest

from mnesis import authz, config, identity, store, tenancy


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """A data root with two tenants, several vaults, and grants:

    tenant ``acme``: alice owns ``alice-priv`` + ``shared``; bob owns ``bob-priv``;
    bob is granted ``shared``. tenant ``other``: carol owns ``other-secret``.
    """
    root = tmp_path / "data"
    monkeypatch.setattr(config, "DATA_ROOT", root, raising=False)
    monkeypatch.setattr(config, "MNESIS_LLM_STUB", True, raising=False)

    tenancy.create_tenant("acme", data_root=root)
    tenancy.create_vault("acme", "shared", owner_principal="alice", data_root=root)
    tenancy.create_vault("acme", "alice-priv", owner_principal="alice", data_root=root)
    tenancy.create_vault("acme", "bob-priv", owner_principal="bob", data_root=root)
    authz.grant_vault_access("acme", "bob", "shared", data_root=root)

    tenancy.create_tenant("other", data_root=root)
    tenancy.create_vault("other", "other-secret", owner_principal="carol", data_root=root)

    alice = identity.Principal(principal_id="alice", tenant_id="acme", role="member")
    bob = identity.Principal(principal_id="bob", tenant_id="acme", role="member")
    carol = identity.Principal(principal_id="carol", tenant_id="other", role="member")
    return root, alice, bob, carol


def _reach(principal, vault_id, root, *, store_counter=None):
    """Boundary-style: authorize the selection, then (only then) open a store."""
    ctx = authz.resolve_vault(principal, vault_id, data_root=root)  # raises Deny on refusal
    with tenancy.use(ctx):
        return [p.id for p in store.list_pages()]


# ── a principal reaches only granted vaults ─────────────────────────────────


def test_principal_reaches_only_its_granted_vaults(env):
    root, alice, bob, carol = env
    # alice owns shared, alice-priv, and the transparent default → all resolvable.
    for vid in ("default", "shared", "alice-priv"):
        ctx = authz.resolve_vault(alice, vid, data_root=root)
        assert ctx.tenant_id == "acme" and ctx.vault_id == vid
    assert authz.accessible_vaults(alice, data_root=root) == {"default", "shared", "alice-priv"}
    # bob owns bob-priv and is granted shared (+ default), but not alice-priv.
    assert authz.accessible_vaults(bob, data_root=root) == {"default", "shared", "bob-priv"}


def test_selecting_an_ungranted_vault_is_denied_and_opens_no_store(env, monkeypatch):
    root, alice, bob, carol = env
    # Count Store constructions — a denied resolution must open none.
    opened = {"n": 0}
    orig_init = store.Store.__init__

    def _counting_init(self, ctx):
        opened["n"] += 1
        return orig_init(self, ctx)

    monkeypatch.setattr(store.Store, "__init__", _counting_init)

    # bob is NOT granted alice-priv (alice owns it, no grant to bob).
    with pytest.raises(identity.Deny) as ei:
        _reach(bob, "alice-priv", root)
    assert ei.value.reason == "vault_forbidden"
    assert opened["n"] == 0  # fail closed BEFORE any store is opened


# ── a vault from another tenant is denied even if it exists ─────────────────


def test_cross_tenant_vault_is_denied_even_though_it_exists(env):
    root, alice, bob, carol = env
    # `other-secret` really exists — but only under tenant `other`. alice (acme) can
    # never even name it; the tenant is credential-derived, never selectable.
    with pytest.raises(identity.Deny) as ei:
        authz.resolve_vault(alice, "other-secret", data_root=root)
    assert ei.value.reason == "unknown_vault"
    # carol reaches it fine within her own tenant.
    assert authz.resolve_vault(carol, "other-secret", data_root=root).tenant_id == "other"


def test_same_named_vault_stays_tenant_isolated(env):
    root, alice, bob, carol = env
    # Both tenants get a vault named `team`; each principal reaches only their own.
    tenancy.create_vault("acme", "team", owner_principal="alice", data_root=root)
    tenancy.create_vault("other", "team", owner_principal="carol", data_root=root)
    a = authz.resolve_vault(alice, "team", data_root=root)
    c = authz.resolve_vault(carol, "team", data_root=root)
    assert a.tenant_id == "acme" and c.tenant_id == "other"
    assert a.root_path != c.root_path


# ── unauthenticated / unresolved fails closed ───────────────────────────────


def test_unauthenticated_resolution_fails_closed(env):
    root, *_ = env
    with pytest.raises(identity.Deny) as ei:
        authz.resolve_vault(None, "shared", data_root=root)
    assert ei.value.reason == "no_principal"
    # Even for the default vault — no principal, no vault.
    with pytest.raises(identity.Deny):
        authz.resolve_vault(None, None, data_root=root)


def test_unknown_vault_is_denied_with_no_default_fallback(env):
    root, alice, *_ = env
    with pytest.raises(identity.Deny) as ei:
        authz.resolve_vault(alice, "does-not-exist", data_root=root)
    assert ei.value.reason == "unknown_vault"  # NOT silently downgraded to `default`


# ── switching between two granted vaults works and stays isolated ───────────


def test_switching_between_granted_vaults_is_isolated(env):
    root, alice, bob, carol = env
    # alice writes distinct pages into two vaults she may reach.
    with tenancy.use(authz.resolve_vault(alice, "shared", data_root=root)):
        store.write_page(store.Page(id="p", title="in shared", body="s"))
    with tenancy.use(authz.resolve_vault(alice, "alice-priv", data_root=root)):
        store.write_page(store.Page(id="p", title="in private", body="p"))
    # Reading back through the authorized resolver shows fully isolated content.
    with tenancy.use(authz.resolve_vault(alice, "shared", data_root=root)):
        assert store.read_page("p").title == "in shared"
    with tenancy.use(authz.resolve_vault(alice, "alice-priv", data_root=root)):
        assert store.read_page("p").title == "in private"
    # bob (granted shared) sees alice's shared write; he cannot reach alice-priv at all.
    assert _reach(bob, "shared", root) == ["p"]
    with pytest.raises(identity.Deny):
        _reach(bob, "alice-priv", root)


# ── a crafted request cannot escalate to an ungranted vault ─────────────────


def test_crafted_requests_cannot_escalate(env):
    root, alice, bob, carol = env
    # Path-traversal / separator ids can never even name a vault → invalid, fail closed.
    for crafted in ("../other/vaults/other-secret", "..", "a/b", "a\\b", "/etc/passwd", ""):
        with pytest.raises(identity.Deny) as ei:
            authz.resolve_vault(alice, crafted, data_root=root)
        assert ei.value.reason in {"invalid_vault"}
    # A real but ungranted vault is refused (no ownership, no grant).
    with pytest.raises(identity.Deny) as ei:
        authz.resolve_vault(bob, "alice-priv", data_root=root)
    assert ei.value.reason == "vault_forbidden"
    # Revoking a grant takes effect immediately (fail closed thereafter).
    assert authz.resolve_vault(bob, "shared", data_root=root).vault_id == "shared"
    authz.revoke_vault_access("acme", "bob", "shared", data_root=root)
    with pytest.raises(identity.Deny) as ei:
        authz.resolve_vault(bob, "shared", data_root=root)
    assert ei.value.reason == "vault_forbidden"


# ── the PDP includes the vault-access check (defense in depth) ──────────────


def test_pdp_denies_actions_on_an_ungranted_bound_vault(env):
    root, alice, bob, carol = env
    # Bind the vault bob may NOT reach; the PDP must deny even a read.
    ungranted = tenancy.context_for("acme", "alice-priv", data_root=root)
    with tenancy.use(ungranted):
        d = authz.decide(bob, authz.READ)
        assert not d.allowed and d.reason == "vault_forbidden"
        # alice (owner) is allowed on the same bound vault.
        assert authz.decide(alice, authz.READ).allowed
    # A granted vault authorizes normally through the PDP.
    with tenancy.use(tenancy.context_for("acme", "shared", data_root=root)):
        assert authz.decide(bob, authz.READ).allowed


def test_per_vault_role_defaults_to_the_tenant_role(env):
    root, alice, bob, carol = env
    # No explicit per-vault role → the principal's tenant role.
    assert authz.vault_role(alice, "shared", data_root=root) == "member"
    # An explicit per-vault grant role narrows/overrides it for that vault only.
    authz.grant_vault_access("acme", "bob", "alice-priv", role="readonly", data_root=root)
    assert authz.vault_role(bob, "alice-priv", data_root=root) == "readonly"
    assert authz.vault_role(bob, "shared", data_root=root) == "member"  # unaffected
