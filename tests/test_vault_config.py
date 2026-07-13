"""V3 — per-vault configuration: predicates, entity types, schema (CLAUDE.md §16 Vaults).

Each vault may organize knowledge differently. The typed-graph schema (entity types +
predicates) and related knowledge-organization settings are **per-vault**, stored under
the vault root, with the current global schema as the default. The pipeline reads the
ACTIVE vault's config: extraction/validation and the graph validate against that vault's
schema only; editing vault A never touches vault B; a migrated vault carries the default.
"""

from __future__ import annotations

import pytest

from mnesis import config, graph, ingest, store, tenancy, vocab

# The SAME input for every vault. The offline stub turns `rel{s|p|o}`/`tag{...}` markers
# into deterministic extracted relations/tags, so the only variable is the vault's schema.
SRC = (
    "Acme employs Bob and Atlas uses Redis. "
    "rel{org:acme|employs|person:bob} "
    "rel{project:atlas|uses|library:redis} "
    "tag{org:acme} tag{library:redis}"
)


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """Tenant ``acme`` with two vaults of DIFFERENT schemas:
    ``crm`` (entity types person/org/project/library; predicates employs/uses) and
    ``eng`` (the default schema)."""
    root = tmp_path / "data"
    monkeypatch.setattr(config, "DATA_ROOT", root, raising=False)
    monkeypatch.setattr(config, "MNESIS_LLM_STUB", True, raising=False)
    tenancy.create_tenant("acme", data_root=root)  # provisions the default vault too
    crm = tenancy.create_vault("acme", "crm", data_root=root)
    eng = tenancy.create_vault("acme", "eng", data_root=root)
    vocab.save_config(
        crm,
        vocab.VaultConfig(
            entity_types=("person", "org", "project", "library"),
            predicates=("employs", "uses"),
        ),
    )
    return root, crm, eng


# ── each vault carries its own schema; new/default vaults get the default ────


def test_each_vault_has_its_own_schema(env):
    root, crm, eng = env
    ca, cb = vocab.load_config(crm), vocab.load_config(eng)
    # crm's custom schema.
    assert "employs" in ca.predicates and "org" in ca.entity_types
    # eng gets the DEFAULT schema (equal to the current global schema).
    assert "employs" not in cb.predicates and "org" not in cb.entity_types
    assert set(cb.entity_types) == set(vocab.DEFAULT_ENTITY_TYPES)
    assert cb.to_dict() == vocab.default_config().to_dict()
    # The config lives UNDER the vault root, per vault, isolated on disk.
    assert crm.config_path != eng.config_path
    assert crm.config_path.is_relative_to(crm.root_path)


# ── the active vault's schema governs validation ────────────────────────────


def test_relation_validation_follows_the_active_vault_schema(env):
    root, crm, eng = env
    rel = {"s": "org:acme", "p": "employs", "o": "person:bob"}
    with tenancy.use(crm):
        assert vocab.validate_relation(rel) == rel  # valid under crm
    with tenancy.use(eng):
        with pytest.raises(ValueError):  # `org`/`employs` unknown under the default schema
            vocab.validate_relation(rel)


# ── the same input yields schema-appropriate extraction + graph per vault ────


def test_same_input_extracts_and_builds_graph_per_vault(env):
    root, crm, eng = env

    with tenancy.use(crm):
        pa = ingest.ingest_source(SRC, "s1")
        graph.rebuild_graph()
        crm_preds = {r["p"] for r in pa.relations}
        crm_has_org = graph.entity("org:acme") is not None
        crm_employs = [n for n in graph.neighbors("org:acme") if n.get("predicate") == "employs"]

    with tenancy.use(eng):
        pb = ingest.ingest_source(SRC, "s1")
        graph.rebuild_graph()
        eng_preds = {r["p"] for r in pb.relations}
        eng_has_org = graph.entity("org:acme") is not None

    # crm's schema accepts both relations, incl. the org→person `employs` edge.
    assert crm_preds == {"employs", "uses"}
    assert crm_has_org and crm_employs and crm_employs[0]["ref"] == "person:bob"

    # eng's default schema keeps only the schema-valid `uses` edge; `employs`/`org`
    # are unknown there, so that relation is dropped and no org node is projected.
    assert eng_preds == {"uses"}
    assert not eng_has_org

    # Tolerate-and-flag: the `org:acme` tag is not lost even where `org` is unknown —
    # it is kept as a free tag on the page (no data loss), just not a graph entity.
    assert "org:acme" in pb.tags


# ── config edits are isolated: changing A never touches B ────────────────────


def test_editing_one_vault_config_leaves_the_other_unchanged(env):
    root, crm, eng = env
    before_eng = vocab.load_config(eng).to_dict()

    ca = vocab.load_config(crm)
    vocab.save_config(
        crm, vocab.VaultConfig(entity_types=ca.entity_types, predicates=(*ca.predicates, "mentors"))
    )
    assert "mentors" in vocab.load_config(crm).predicates  # edit took effect for crm
    assert vocab.load_config(eng).to_dict() == before_eng  # eng is byte-for-byte unchanged

    # And the edit is reflected live in the active-schema validation for crm only.
    with tenancy.use(crm):
        assert vocab.is_valid_predicate("mentors")
    with tenancy.use(eng):
        assert not vocab.is_valid_predicate("mentors")


# ── a migrated vault carries the default schema and behaves as before ────────


def test_migrated_vault_carries_default_schema_and_behaves_as_before(tmp_path, monkeypatch):
    root = tmp_path / "data"
    monkeypatch.setattr(config, "DATA_ROOT", root, raising=False)
    monkeypatch.setattr(config, "MNESIS_LLM_STUB", True, raising=False)

    # A pre-vault per-tenant store, then migrate it into the default vault.
    tdir = root / config.TENANTS_DIRNAME / "acme"
    (tdir / "pages").mkdir(parents=True)
    (tdir / "sources").mkdir(parents=True)
    import subprocess

    subprocess.run(["git", "-C", str(tdir), "init", "-q"], check=True)
    tenancy.migrate_tenant_to_default_vault("acme", data_root=root)

    vctx = tenancy.context_for("acme", "default", data_root=root)
    # The migrated vault carries the DEFAULT schema.
    assert vctx.config_path.is_file()
    assert vocab.load_config(vctx).to_dict() == vocab.default_config().to_dict()

    # And it behaves exactly as the pre-vault (global-schema) pipeline did: the classic
    # default-schema relations validate and land on the page.
    with tenancy.use(vctx):
        page = ingest.ingest_source(
            "Atlas uses Redis. rel{project:atlas|uses|library:redis}", "s1"
        )
        assert {"s": "project:atlas", "p": "uses", "o": "library:redis"} in page.relations
