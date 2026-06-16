"""Agent Skills subsystem — conformant with the agentskills.io standard.

A *skill* is a folder containing a ``SKILL.md`` file (YAML frontmatter + Markdown
instructions), optionally bundling ``scripts/``, ``references/`` and ``assets/``.
This is the same SKILL.md format Claude Code uses.

Spec: https://agentskills.io  (SKILL.md schema; three-level progressive disclosure).

Progressive disclosure — strictly enforced here:
  1. **Discovery (metadata only).** Scan skill dirs, parse ONLY each SKILL.md's
     YAML frontmatter (`name` + `description` required), register a lightweight
     ``SkillCard{name, description, path}``. The instruction body is never read.
  2. **Activation (full instructions).** ``activate(name)`` / ``load_skill(path)``
     reads the full SKILL.md (frontmatter + body) on demand.
  3. **Resources (on demand).** The activated skill can read its ``references/``
     and ``assets/`` and run its ``scripts/`` only when the instructions call for
     it — each access path-confined to the skill folder and bounded.

Exposure to a model (model-agnostic, any LLM provider):
  - **Discovery context:** ``SkillRegistry.cards_prompt()`` lists name+description
    so the model knows what skills exist and when to use them (level 1, cheap).
  - **Activation tool:** ``make_use_skill_tool(registry)`` is a LangChain
    ``use_skill(name)`` tool; calling it loads that skill's instructions into the
    conversation (level 2). Resource/script use (level 3) follows the loaded
    instructions via the activated skill's guarded methods.

Safety posture: bundled-script execution is opt-in and guarded — no shell, the
command/cwd are confined to the skill folder, a wall-clock timeout bounds it, and
output is size-capped. Nothing here auto-executes a script; the agent harness
decides, and even then only within these bounds. (See ``ActivatedSkill.run_script``.)
"""
from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .. import config

SKILL_FILE = "SKILL.md"


# ── Errors ────────────────────────────────────────────────────────────────


class SkillError(Exception):
    """Base class for skill subsystem errors."""


class SkillFormatError(SkillError):
    """A SKILL.md is malformed or missing required fields (name/description)."""


class SkillSecurityError(SkillError):
    """A resource/script path escaped the skill folder."""


class SkillExecutionError(SkillError):
    """A bundled script could not be run within the guards."""


@dataclass(frozen=True)
class DiscoveryIssue:
    """A skill folder that failed discovery — reported, never crashes the scan."""

    path: Path
    reason: str


# ── Level 1: discovery (metadata only) ──────────────────────────────────────


@dataclass(frozen=True)
class SkillCard:
    """Lightweight skill metadata loaded at discovery: name + description + path.

    The instruction body is deliberately NOT loaded here (progressive disclosure).
    """

    name: str
    description: str
    path: Path


def _read_frontmatter_only(skill_md: Path) -> dict[str, Any]:
    """Parse ONLY the YAML frontmatter block, stopping at its closing ``---``.

    The Markdown body is never read into memory — this is what keeps discovery to
    metadata only. Raises SkillFormatError if there is no terminated frontmatter.
    """
    lines: list[str] = []
    with open(skill_md, encoding="utf-8") as f:
        if f.readline().strip() != "---":
            raise SkillFormatError("SKILL.md must begin with a '---' YAML frontmatter block")
        for line in f:
            if line.strip() == "---":
                break
            lines.append(line)
        else:
            raise SkillFormatError("SKILL.md frontmatter is not terminated by '---'")
    data = yaml.safe_load("".join(lines))
    if not isinstance(data, dict):
        raise SkillFormatError("SKILL.md frontmatter must be a YAML mapping")
    return data


def discover_skills(dirs: list[Path]) -> tuple[list[SkillCard], list[DiscoveryIssue]]:
    """Scan ``dirs`` for skill folders, returning (cards, issues).

    A skill folder is any subdirectory containing a ``SKILL.md``. Reads only the
    frontmatter; requires ``name`` and ``description``. Optional spec fields
    (version, license, allowed-tools, …) are tolerated and ignored at this stage
    (they're available after activation). Duplicate names: first one wins; the
    later one is reported as an issue. Malformed skills are reported, not raised.
    """
    cards: list[SkillCard] = []
    issues: list[DiscoveryIssue] = []
    seen: set[str] = set()

    for d in dirs:
        if not d.is_dir():
            continue
        for sub in sorted(d.iterdir()):
            skill_md = sub / SKILL_FILE
            if not (sub.is_dir() and skill_md.is_file()):
                continue
            try:
                fm = _read_frontmatter_only(skill_md)
                name, description = fm.get("name"), fm.get("description")
                if not name or not str(name).strip():
                    raise SkillFormatError("SKILL.md frontmatter requires a non-empty 'name'")
                if not description or not str(description).strip():
                    raise SkillFormatError("SKILL.md frontmatter requires a non-empty 'description'")
                name = str(name).strip()
                if name in seen:
                    raise SkillFormatError(f"duplicate skill name {name!r} (first definition wins)")
                seen.add(name)
                cards.append(SkillCard(name=name, description=str(description).strip(), path=sub))
            except SkillError as exc:
                issues.append(DiscoveryIssue(path=skill_md, reason=str(exc)))
            except Exception as exc:  # malformed YAML, encoding, etc.
                issues.append(DiscoveryIssue(path=skill_md, reason=f"could not parse SKILL.md: {exc}"))
    return cards, issues


# ── Level 2/3: activation + resources/scripts ───────────────────────────────


def _split_skill_md(skill_md: Path) -> tuple[dict[str, Any], str]:
    """Read the full SKILL.md → (frontmatter dict, instruction body)."""
    text = skill_md.read_text(encoding="utf-8")
    if not text.startswith("---"):
        return {}, text.strip()
    lines = text.splitlines(keepends=True)
    end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    if end is None:
        return {}, text.strip()
    fm = yaml.safe_load("".join(lines[1:end])) or {}
    body = "".join(lines[end + 1 :]).strip()
    return (fm if isinstance(fm, dict) else {}), body


@dataclass(frozen=True)
class ActivatedSkill:
    """A fully-loaded skill: instructions + frontmatter, with guarded access to
    its bundled resources and scripts (progressive-disclosure level 3)."""

    name: str
    description: str
    instructions: str
    frontmatter: dict[str, Any]
    path: Path

    @property
    def allowed_tools(self) -> list[str]:
        """Tools the skill declares it may use (``allowed-tools``/``allowed_tools``)."""
        v = self.frontmatter.get("allowed-tools", self.frontmatter.get("allowed_tools"))
        if isinstance(v, str):
            return [t.strip() for t in v.split(",") if t.strip()]
        if isinstance(v, list):
            return [str(t).strip() for t in v if str(t).strip()]
        return []

    def _safe_path(self, relpath: str) -> Path:
        """Resolve ``relpath`` inside the skill folder, refusing any escape."""
        base = self.path.resolve()
        target = (base / relpath).resolve()
        if not target.is_relative_to(base):
            raise SkillSecurityError(f"resource path escapes skill folder: {relpath!r}")
        return target

    def read_resource(self, relpath: str) -> str:
        """Read a bundled reference/asset file (text), confined to the skill folder."""
        target = self._safe_path(relpath)
        if not target.is_file():
            raise FileNotFoundError(f"no such resource in skill {self.name!r}: {relpath}")
        return target.read_text(encoding="utf-8")

    def run_script(
        self,
        relpath: str,
        args: list[str] | None = None,
        *,
        timeout: float = 30.0,
        max_output_bytes: int = 64_000,
    ) -> dict[str, Any]:
        """Run a bundled script under guards (no shell, confined cwd, timeout, capped output).

        Supports ``.py`` (via this interpreter), ``.sh`` (via ``sh``), or an
        already-executable file. Returns ``{returncode, stdout, stderr}``.
        """
        script = self._safe_path(relpath)
        if not script.is_file():
            raise FileNotFoundError(f"no such script in skill {self.name!r}: {relpath}")
        argv = [str(a) for a in (args or [])]
        if script.suffix == ".py":
            cmd = [sys.executable, str(script), *argv]
        elif script.suffix == ".sh":
            cmd = ["sh", str(script), *argv]
        elif os.access(script, os.X_OK):
            cmd = [str(script), *argv]
        else:
            raise SkillExecutionError(
                f"unsupported/non-executable script {relpath!r} (use .py, .sh, or chmod +x)"
            )
        try:
            proc = subprocess.run(
                cmd, cwd=str(self.path), capture_output=True, text=True,
                timeout=timeout, shell=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise SkillExecutionError(f"script {relpath!r} exceeded {timeout}s timeout") from exc
        return {
            "returncode": proc.returncode,
            "stdout": (proc.stdout or "")[:max_output_bytes],
            "stderr": (proc.stderr or "")[:max_output_bytes],
        }


def load_skill(path: Path) -> ActivatedSkill:
    """Activate a skill folder: read its full SKILL.md instructions on demand."""
    skill_md = path / SKILL_FILE
    if not skill_md.is_file():
        raise SkillFormatError(f"no {SKILL_FILE} in {path}")
    fm, body = _split_skill_md(skill_md)
    name = str(fm.get("name", path.name)).strip()
    description = str(fm.get("description", "")).strip()
    return ActivatedSkill(
        name=name, description=description, instructions=body, frontmatter=fm, path=path,
    )


# ── Directories + registry ──────────────────────────────────────────────────


def default_skill_dirs() -> list[Path]:
    """Skill directories scanned by default: any ``MNESIS_AGENTS_SKILLS_DIRS``
    entries, then project-level ``./skills``, then the packaged example skills."""
    dirs: list[Path] = []
    if config.MNESIS_AGENTS_SKILLS_DIRS:
        dirs += [Path(p) for p in config.MNESIS_AGENTS_SKILLS_DIRS.split(os.pathsep) if p.strip()]
    dirs.append(Path.cwd() / "skills")
    dirs.append(Path(__file__).resolve().parent / "bundled")  # packaged examples
    return dirs


@dataclass
class SkillRegistry:
    """Discovers and activates skills for a base agent to consume.

    Discovery is metadata-only; ``activate`` loads full instructions on demand.
    """

    dirs: list[Path] = field(default_factory=default_skill_dirs)
    cards: list[SkillCard] = field(default_factory=list)
    issues: list[DiscoveryIssue] = field(default_factory=list)

    def discover(self) -> "SkillRegistry":
        self.cards, self.issues = discover_skills(self.dirs)
        return self

    def names(self) -> list[str]:
        return [c.name for c in self.cards]

    def card(self, name: str) -> SkillCard:
        for c in self.cards:
            if c.name == name:
                return c
        raise KeyError(name)

    def activate(self, name: str) -> ActivatedSkill:
        """Load the named skill's full instructions (progressive-disclosure level 2)."""
        return load_skill(self.card(name).path)

    def cards_prompt(self) -> str:
        """A compact 'available skills' block for the system prompt (discovery)."""
        if not self.cards:
            return "No agent skills are available."
        lines = ["Available agent skills (call use_skill(name) to load one's instructions):"]
        lines += [f"- {c.name}: {c.description}" for c in self.cards]
        return "\n".join(lines)


# ── Model exposure: the use_skill tool ──────────────────────────────────────


def make_use_skill_tool(registry: "SkillRegistry"):
    """A LangChain ``use_skill(name)`` tool that activates a skill and returns its
    instructions for the model to follow. Unknown names return an actionable
    message (with the available names) rather than raising, so the model recovers.
    """
    from langchain_core.tools import tool

    @tool
    def use_skill(name: str) -> str:
        """Activate an Agent Skill by name to load its full instructions into context.
        Use when one of the listed skills matches the task. Returns the skill's instructions."""
        try:
            skill = registry.activate(name)
        except KeyError:
            avail = ", ".join(registry.names()) or "(none)"
            return f"No skill named {name!r}. Available skills: {avail}."
        header = f"# Skill activated: {skill.name}\n\n"
        return header + (skill.instructions or "(this skill has no instruction body)")

    return use_skill
