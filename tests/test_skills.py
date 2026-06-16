"""Tests for the Agent Skills subsystem (agentskills.io progressive disclosure).

Offline, no model. Uses temp skill folders for precise control, plus the real
bundled example skill to validate discovery/activation end to end.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from mnesis_agents.skills.loader import (
    ActivatedSkill,
    DiscoveryIssue,
    SkillCard,
    SkillRegistry,
    SkillSecurityError,
    discover_skills,
    load_skill,
    make_use_skill_tool,
)


def _write_skill(
    root: Path, folder: str, *, name: str | None = "demo", description: str | None = "A demo.",
    extra_frontmatter: str = "", body: str = "Body instructions.",
) -> Path:
    """Write a skill folder with a SKILL.md; name/description omitted if None."""
    d = root / folder
    d.mkdir(parents=True)
    fm = ["---"]
    if name is not None:
        fm.append(f"name: {name}")
    if description is not None:
        fm.append(f"description: {description}")
    if extra_frontmatter:
        fm.append(extra_frontmatter.rstrip())
    fm.append("---")
    (d / "SKILL.md").write_text("\n".join(fm) + "\n" + body, encoding="utf-8")
    return d


# ── Level 1: discovery (metadata only) ──────────────────────────────────────


def test_discovery_registers_card_with_name_and_description(tmp_path):
    _write_skill(tmp_path, "alpha", name="alpha", description="Does alpha things.")
    cards, issues = discover_skills([tmp_path])
    assert issues == []
    assert len(cards) == 1
    c = cards[0]
    assert isinstance(c, SkillCard)
    assert c.name == "alpha" and c.description == "Does alpha things."
    assert c.path == tmp_path / "alpha"


def test_discovery_does_not_load_the_body(tmp_path):
    # A unique sentinel in the body must NOT appear anywhere in the discovered
    # card — discovery parses frontmatter only (progressive disclosure level 1).
    sentinel = "ZZ_BODY_SENTINEL_42"
    _write_skill(tmp_path, "alpha", body=f"# Title\n\n{sentinel} instructions here.")
    cards, _ = discover_skills([tmp_path])
    assert sentinel not in repr(cards[0])
    assert not hasattr(cards[0], "instructions") and not hasattr(cards[0], "body")
    # …but it IS present once activated.
    assert sentinel in load_skill(cards[0].path).instructions


def test_discovery_tolerates_optional_spec_fields(tmp_path):
    _write_skill(
        tmp_path, "alpha",
        extra_frontmatter="version: 1.2.0\nlicense: MIT\nallowed-tools:\n  - mnesis_get",
    )
    cards, issues = discover_skills([tmp_path])
    assert issues == [] and len(cards) == 1  # optional fields don't break discovery


def test_discovery_ignores_non_skill_folders(tmp_path):
    (tmp_path / "not-a-skill").mkdir()  # no SKILL.md
    (tmp_path / "loose.txt").write_text("x")
    cards, issues = discover_skills([tmp_path])
    assert cards == [] and issues == []


# ── Invalid SKILL.md → reported, not crashed ────────────────────────────────


def test_missing_name_is_reported_not_raised(tmp_path):
    _write_skill(tmp_path, "bad", name=None, description="has desc, no name")
    _write_skill(tmp_path, "good", name="good", description="fine")
    cards, issues = discover_skills([tmp_path])
    assert {c.name for c in cards} == {"good"}  # scan continues past the bad one
    assert len(issues) == 1 and isinstance(issues[0], DiscoveryIssue)
    assert "name" in issues[0].reason


def test_missing_description_is_reported(tmp_path):
    _write_skill(tmp_path, "bad", name="bad", description=None)
    cards, issues = discover_skills([tmp_path])
    assert cards == [] and len(issues) == 1 and "description" in issues[0].reason


def test_no_frontmatter_is_reported(tmp_path):
    d = tmp_path / "nofm"
    d.mkdir()
    (d / "SKILL.md").write_text("# Just a body, no frontmatter\n", encoding="utf-8")
    cards, issues = discover_skills([tmp_path])
    assert cards == [] and len(issues) == 1


def test_duplicate_name_reported_first_wins(tmp_path):
    a = tmp_path / "a"
    b = tmp_path / "b"
    for folder in (a, b):
        folder.mkdir()
        (folder / "SKILL.md").write_text("---\nname: dup\ndescription: d\n---\nx", encoding="utf-8")
    cards, issues = discover_skills([tmp_path])
    assert len(cards) == 1 and cards[0].name == "dup"
    assert len(issues) == 1 and "duplicate" in issues[0].reason


# ── Level 2/3: activation + resources + scripts ─────────────────────────────


def test_activation_loads_full_instructions_and_frontmatter(tmp_path):
    d = _write_skill(
        tmp_path, "alpha",
        extra_frontmatter="allowed-tools:\n  - mnesis_get\n  - mnesis_query",
        body="# Alpha\n\nStep one. Step two.",
    )
    sk = load_skill(d)
    assert isinstance(sk, ActivatedSkill)
    assert "Step one" in sk.instructions
    assert sk.allowed_tools == ["mnesis_get", "mnesis_query"]


def test_referenced_file_resolves(tmp_path):
    d = _write_skill(tmp_path, "alpha")
    (d / "references").mkdir()
    (d / "references" / "style.md").write_text("# Style\nbe concise", encoding="utf-8")
    sk = load_skill(d)
    assert "be concise" in sk.read_resource("references/style.md")


def test_path_escape_is_blocked(tmp_path):
    d = _write_skill(tmp_path, "alpha")
    sk = load_skill(d)
    with pytest.raises(SkillSecurityError):
        sk.read_resource("../../etc/passwd")


def test_bundled_script_runs_within_guards(tmp_path):
    d = _write_skill(tmp_path, "alpha")
    (d / "scripts").mkdir()
    (d / "scripts" / "echo.py").write_text(
        "import sys; print('words', len(sys.argv) - 1)", encoding="utf-8"
    )
    sk = load_skill(d)
    res = sk.run_script("scripts/echo.py", ["a", "b"])
    assert res["returncode"] == 0 and res["stdout"].strip() == "words 2"


def test_script_path_escape_blocked(tmp_path):
    sk = load_skill(_write_skill(tmp_path, "alpha"))
    with pytest.raises(SkillSecurityError):
        sk.run_script("../evil.py")


# ── Registry + bundled example + exposure ───────────────────────────────────


def test_bundled_example_skill_discovers_and_activates():
    reg = SkillRegistry().discover()  # default dirs include the packaged examples
    assert "summarize-source" in reg.names()
    sk = reg.activate("summarize-source")
    assert "summarize" in sk.instructions.lower()
    assert "mnesis_get" in sk.allowed_tools
    # The bundled reference resolves to its own content (level-3 on-demand).
    assert "style guide" in sk.read_resource("references/style.md").lower()
    out = sk.run_script("scripts/wordcount.py", ["references/style.md"])
    assert out["returncode"] == 0 and out["stdout"].strip().isdigit()


def test_registry_cards_prompt_lists_name_and_description(tmp_path):
    _write_skill(tmp_path, "alpha", name="alpha", description="Does alpha.")
    reg = SkillRegistry(dirs=[tmp_path]).discover()
    text = reg.cards_prompt()
    assert "alpha" in text and "Does alpha." in text and "use_skill" in text


def test_use_skill_tool_activates_and_returns_instructions(tmp_path):
    _write_skill(tmp_path, "alpha", name="alpha", description="d", body="ALPHA-INSTRUCTIONS")
    reg = SkillRegistry(dirs=[tmp_path]).discover()
    tool = make_use_skill_tool(reg)
    assert tool.name == "use_skill"
    out = tool.invoke({"name": "alpha"})
    assert "ALPHA-INSTRUCTIONS" in out and "alpha" in out

    miss = tool.invoke({"name": "nope"})
    assert "No skill named" in miss and "alpha" in miss  # lists available, no raise


def test_registry_activate_unknown_raises_keyerror(tmp_path):
    reg = SkillRegistry(dirs=[tmp_path]).discover()
    with pytest.raises(KeyError):
        reg.activate("nope")
