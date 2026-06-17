"""The concrete MaintenanceAgent — Mnesis's scheduled "dream cycle".

A dream cycle is a deterministic, governed sweep of curation **passes**, each
driven by one of the M2 maintenance Agent Skills over the M1 Mnesis maintenance
MCP tools. Maintenance is mechanical (like the ingest-daemon, it is *not* an LLM
reasoning loop): the cycle is a LangGraph graph whose nodes run the passes in
order, auto-applying the safe-hygiene ops and accumulating every
knowledge-changing op as a **proposal** — never applying it.

Layering:
  - **F4** — :class:`DreamMaintenanceAgent` is a concrete ``MaintenanceAgent``
    (schedule-triggered, ``write_policy="propose"``). It still ``build()``s a
    normal base agent for ad-hoc maintenance chat; ``run_dream_cycle`` is the
    scheduled, deterministic orchestrator on top.
  - **F6** — every tool call goes through :class:`GovernanceMiddleware`: the safe
    writers (``mnesis_decay``, ``mnesis_graph_lint``) are not in ``write_tools`` so
    they execute (auto-apply), while the knowledge-changing writes
    (``mnesis_resolve``/``mnesis_ingest``/``mnesis_file_back``) are gated by the
    ``propose`` policy and never fire. Budgets (tool-call / wall-clock) stop the
    cycle deterministically.
  - **F2** — reaches Mnesis only through the injected MCP tools; imports nothing
    from the ``mnesis`` package.

Each pass is **resilient**: a failure (missing skill, a raising tool, a bad
script) is recorded on that pass and the cycle continues. The structured per-pass
output is produced by the skill's own helper script, so the policy
(auto-apply vs propose) lives in the skill, not here.

Note: the pass procedures consume the **structured (JSON)** tool outputs that the
fake source mirrors; bridging the human-readable text the live M1 tools return is
a thin, separable parsing adapter (a parse failure is just a recorded pass error,
never a crash).
"""
from __future__ import annotations

import json
import operator
import tempfile
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, Callable, TypedDict

from . import config
from .categories.maintenance import MaintenanceAgent
from .governance import GovernanceMiddleware
from .knowledge import MAINTENANCE_TOOL_NAMES
from .skills.loader import SkillRegistry

if TYPE_CHECKING:
    from langchain_core.tools import BaseTool

#: The default ordered dream-cycle plan: audit first, then age, tidy the graph,
#: then the two proposal-only consolidation passes.
DEFAULT_PLAN: tuple[str, ...] = (
    "quality-sweep",
    "decay-sweep",
    "graph-hygiene",
    "contradiction-triage",
    "deduplication",
)

#: Knowledge-changing writes the dream cycle must only ever PROPOSE (gated by F6).
#: The safe hygiene writers (mnesis_decay, mnesis_graph_lint) are deliberately
#: absent, so governance lets them auto-apply.
KNOWLEDGE_WRITE_TOOLS: frozenset[str] = frozenset(
    {"mnesis_resolve", "mnesis_ingest", "mnesis_file_back"}
)

#: skill -> its deterministic post-processing helper script (M2).
_SKILL_SCRIPTS: dict[str, str] = {
    "quality-sweep": "scripts/findings.py",
    "decay-sweep": "scripts/summarize.py",
    "graph-hygiene": "scripts/summarize.py",
    "contradiction-triage": "scripts/triage.py",
    "deduplication": "scripts/propose.py",
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


# ── Report shape ────────────────────────────────────────────────────────────


@dataclass
class PassResult:
    """One dream-cycle pass outcome."""

    name: str
    status: str                                   # ok | failed | skipped
    summary: dict[str, Any] = field(default_factory=dict)
    auto_applied: list[dict[str, Any]] = field(default_factory=list)
    proposals: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None


@dataclass
class DreamCycleReport:
    """The structured result of a whole dream cycle."""

    started: str
    ended: str
    passes: list[PassResult]
    health_before: dict[str, Any] | None
    health_after: dict[str, Any] | None
    totals: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "started": self.started,
            "ended": self.ended,
            "passes": [asdict(p) for p in self.passes],
            "health_before": self.health_before,
            "health_after": self.health_after,
            "totals": self.totals,
        }


# ── Governed, deterministic tool dispatch ───────────────────────────────────


class _BudgetStop(Exception):
    """A governed call was refused because a budget/wall-clock limit tripped."""


class _PassError(Exception):
    """A governed call was refused for a non-budget reason (allowlist/policy/missing)."""


@dataclass
class _Call:
    name: str
    ok: bool
    output: str | None = None
    refusal: str | None = None


class _GovernedTools:
    """Dispatches the maintenance MCP tools through F6 governance, outside the LLM
    loop. Reuses :meth:`GovernanceMiddleware._gate` so the allowlist, write-policy,
    and tool-call budget are enforced identically to an agent run; wall-clock is
    checked here (the gate's wall-clock hook only fires inside the model loop)."""

    def __init__(self, tools: list["BaseTool"], governance: GovernanceMiddleware) -> None:
        self._by_name: dict[str, BaseTool] = {}
        for t in tools:
            self._by_name[t.name] = t
            self._by_name.setdefault(t.name.split("__", 1)[-1], t)  # tolerate namespacing
        self._gov = governance
        self.executed: list[dict[str, Any]] = []

    def stopped(self) -> bool:
        return bool(self._gov.state.stop_reason)

    def stop_reason(self) -> str | None:
        return self._gov.state.stop_reason

    def call(self, name: str, args: dict[str, Any]) -> _Call:
        # Wall-clock first (the gate doesn't check it outside the model loop).
        g = self._gov
        if g.wallclock and g.state.started and (time.monotonic() - g.state.started) > g.wallclock:
            g.state.stop_reason = g.state.stop_reason or "deadline"
            return _Call(name, ok=False, refusal="wall-clock budget exceeded")

        tc = {"name": name, "args": args, "id": f"dc-{len(self.executed)}"}
        refusal = g._gate(tc)
        if refusal is not None:
            return _Call(name, ok=False, refusal=str(getattr(refusal, "content", refusal)))

        tool = self._by_name.get(name)
        if tool is None:
            return _Call(name, ok=False, refusal=f"tool {name!r} not available")
        output = tool.invoke(args)
        self.executed.append({"tool": name, "args_keys": sorted(args)})
        return _Call(name, ok=True, output=output if isinstance(output, str) else str(output))

    def require(self, name: str, args: dict[str, Any]) -> _Call:
        """Call a tool, raising on refusal so the pass runner can classify it."""
        c = self.call(name, args)
        if not c.ok:
            if self.stopped():
                raise _BudgetStop(self.stop_reason() or "budget")
            raise _PassError(c.refusal or "refused")
        return c


# ── Pass procedures (the tool I/O each skill's procedure prescribes) ─────────
# These mirror the SKILL.md procedures: gather the tool output(s), assemble the
# JSON the skill's helper script expects, and report which calls were *mutating*
# (so an auto-apply pass can list what it applied).

_Procedure = Callable[[_GovernedTools], tuple[dict[str, Any], list[dict[str, Any]]]]


def _proc_quality(gt: _GovernedTools) -> tuple[dict, list[dict]]:
    c = gt.require("mnesis_health_report", {})
    return json.loads(c.output), []


def _proc_decay(gt: _GovernedTools) -> tuple[dict, list[dict]]:
    c = gt.require("mnesis_decay", {})
    return json.loads(c.output), [{"tool": "mnesis_decay", "args_keys": []}]


def _proc_graph_hygiene(gt: _GovernedTools) -> tuple[dict, list[dict]]:
    report = gt.require("mnesis_graph_lint", {"fix": False})   # read-only report
    applied = gt.require("mnesis_graph_lint", {"fix": True})    # safe auto-fixes
    payload = {"report": json.loads(report.output), "applied": json.loads(applied.output)}
    return payload, [{"tool": "mnesis_graph_lint", "args_keys": ["fix"]}]


def _proc_triage(gt: _GovernedTools) -> tuple[dict, list[dict]]:
    c = gt.require("mnesis_review", {})
    review = json.loads(c.output)
    return {"contradictions": review.get("open", [])}, []


def _proc_dedup(gt: _GovernedTools) -> tuple[dict, list[dict]]:
    c = gt.require("mnesis_find_duplicates", {})
    dupes = json.loads(c.output)
    return {"candidates": dupes.get("candidates", []), "strong_threshold": 0.5}, []


_PROCEDURES: dict[str, _Procedure] = {
    "quality-sweep": _proc_quality,
    "decay-sweep": _proc_decay,
    "graph-hygiene": _proc_graph_hygiene,
    "contradiction-triage": _proc_triage,
    "deduplication": _proc_dedup,
}


# ── LangGraph dream-cycle state ─────────────────────────────────────────────


class _DreamState(TypedDict):
    passes: Annotated[list[dict[str, Any]], operator.add]


# ── The agent ───────────────────────────────────────────────────────────────


class DreamMaintenanceAgent(MaintenanceAgent):
    """Concrete dream-cycle maintenance agent (F4 ``MaintenanceAgent``).

    Runs a configurable, ordered plan of skill-driven passes as a LangGraph graph
    under F6 governance, auto-applying safe hygiene and collecting proposals.
    """

    def __init__(
        self,
        *,
        tools: "list[BaseTool] | None" = None,
        skills: SkillRegistry | None = None,
        model=None,
        plan: list[str] | None = None,
        cadence: str = "0 3 * * *",
        max_tool_calls: int | None = None,
        wallclock_seconds: float | None = None,
        max_tokens: int | None = None,
    ) -> None:
        super().__init__(tools=tools, skills=skills or SkillRegistry().discover(), model=model)
        self._plan = list(plan) if plan is not None else list(DEFAULT_PLAN)
        self._cadence = cadence
        self._max_tool_calls = (
            max_tool_calls if max_tool_calls is not None else config.MNESIS_AGENTS_MAX_TOOL_CALLS
        )
        self._wallclock = (
            wallclock_seconds
            if wallclock_seconds is not None
            else config.MNESIS_AGENTS_WALLCLOCK_SECONDS
        )
        self._max_tokens = max_tokens if max_tokens is not None else config.MNESIS_AGENTS_MAX_TOKENS

    # -- F4 contract -----------------------------------------------------------

    def system_prompt(self) -> str:
        return (
            "You are Mnesis's maintenance (dream-cycle) agent. You curate the "
            "knowledge base by running maintenance skills over the Mnesis MCP "
            "tools: auto-apply only safe hygiene (decay, safe graph fixes); for "
            "anything that changes what the knowledge means (resolving "
            "contradictions, merging duplicates), PROPOSE — never apply."
        )

    def cadence(self) -> str:
        return self._cadence

    def scope(self) -> list[str]:
        """What this run may touch: the maintenance passes in its plan."""
        return list(self._plan)

    def write_tools(self) -> frozenset[str]:
        return KNOWLEDGE_WRITE_TOOLS

    # -- the dream cycle -------------------------------------------------------

    def _maintenance_tools(self) -> list["BaseTool"]:
        """The injected tools restricted to the Mnesis maintenance surface."""
        names = set(MAINTENANCE_TOOL_NAMES)
        return [t for t in self._extra_tools if t.name.split("__", 1)[-1] in names]

    def _new_governance(self) -> GovernanceMiddleware:
        gov = GovernanceMiddleware(
            allowlist=frozenset(MAINTENANCE_TOOL_NAMES),
            write_tools=self.write_tools(),
            write_policy="propose",
            max_tool_calls=self._max_tool_calls,
            max_tokens=self._max_tokens,
            wallclock_seconds=self._wallclock,
        )
        gov.begin_run()
        return gov

    def _run_pass(self, name: str, gt: _GovernedTools, tmpdir: str) -> PassResult:
        """Run one pass: activate its skill, dispatch its governed tools, run its
        helper script. Resilient — any failure is recorded, never raised."""
        if gt.stopped():
            return PassResult(name, "skipped", {"reason": gt.stop_reason()}, error=gt.stop_reason())

        proc = _PROCEDURES.get(name)
        if proc is None:
            return PassResult(name, "failed", {"error": f"no procedure for pass {name!r}"},
                              error="no procedure")
        try:
            skill = self._skills.activate(name)            # F3 activation
        except KeyError:
            return PassResult(name, "failed", {"error": f"skill {name!r} not found"},
                              error="skill not found")

        try:
            script_input, mutating = proc(gt)              # governed tool I/O
        except _BudgetStop as exc:
            return PassResult(name, "skipped", {"reason": str(exc)}, error=str(exc))
        except Exception as exc:  # raising tool, parse failure, bad output
            return PassResult(name, "failed", {"error": str(exc)}, error=str(exc))

        try:
            out = self._run_skill_script(skill, name, script_input, tmpdir)
        except Exception as exc:
            return PassResult(name, "failed", {"error": f"script: {exc}"}, error=str(exc))

        action = out.get("action")
        auto = mutating if action == "auto_applied" else []
        proposals = out.get("proposals", []) if action == "propose" else []
        return PassResult(name, "ok", out, auto_applied=auto, proposals=proposals)

    @staticmethod
    def _run_skill_script(skill, name: str, script_input: dict, tmpdir: str) -> dict:
        infile = Path(tmpdir) / f"{name}.json"
        infile.write_text(json.dumps(script_input), encoding="utf-8")
        res = skill.run_script(_SKILL_SCRIPTS[name], [str(infile)])
        if res["returncode"] != 0:
            raise RuntimeError(res.get("stderr", "").strip() or "script failed")
        return json.loads(res["stdout"])

    def _build_graph(self, plan: list[str], gt: _GovernedTools, tmpdir: str):
        """Compile a sequential LangGraph graph: one node per pass, START→…→END."""
        from langgraph.graph import END, START, StateGraph

        def make_node(pass_name: str):
            def node(_state: _DreamState) -> dict:
                return {"passes": [asdict(self._run_pass(pass_name, gt, tmpdir))]}

            return node

        g = StateGraph(_DreamState)
        prev = START
        for i, pass_name in enumerate(plan):
            node_id = f"pass_{i}_{pass_name}"
            g.add_node(node_id, make_node(pass_name))
            g.add_edge(prev, node_id)
            prev = node_id
        g.add_edge(prev, END)
        return g.compile()

    def _health(self, gt: _GovernedTools) -> dict | None:
        """A governed health snapshot (None if refused / unparsable)."""
        c = gt.call("mnesis_health_report", {})
        if not c.ok:
            return None
        try:
            return json.loads(c.output)
        except (ValueError, TypeError):
            return None

    def run_dream_cycle(self, plan: list[str] | None = None) -> DreamCycleReport:
        """Run the dream cycle and return a structured :class:`DreamCycleReport`.

        Sequential by default. Auto-applies the safe-hygiene passes through the
        governed tools; accumulates contradiction/dedup proposals without applying
        them. Resilient: a failing pass is recorded and the cycle continues. The
        budget caps from F6 stop it deterministically.
        """
        plan = list(plan) if plan is not None else list(self._plan)
        started = _now_iso()
        gov = self._new_governance()
        gt = _GovernedTools(self._maintenance_tools(), gov)

        with tempfile.TemporaryDirectory(prefix="mnesis-dream-") as tmpdir:
            health_before = self._health(gt)
            state = self._build_graph(plan, gt, tmpdir).invoke({"passes": []})
            passes = [PassResult(**p) for p in state["passes"]]
            health_after = self._health(gt)

        totals = {
            "passes": len(passes),
            "ok": sum(1 for p in passes if p.status == "ok"),
            "failed": sum(1 for p in passes if p.status == "failed"),
            "skipped": sum(1 for p in passes if p.status == "skipped"),
            "auto_applied": sum(len(p.auto_applied) for p in passes),
            "proposals": sum(len(p.proposals) for p in passes),
            "tool_calls": len(gt.executed),
            "tools_called": [c["tool"] for c in gt.executed],
            "stop_reason": gov.state.stop_reason,
        }
        return DreamCycleReport(
            started=started, ended=_now_iso(), passes=passes,
            health_before=health_before, health_after=health_after, totals=totals,
        )
