# Mnesis — Agentic Foundation Build Playbook (LangChain stack)

**Scaffolding and integration for the agentic layer of Mnesis, built on the open-source LangChain / LangGraph stack, multi-LLM by default, and conformant to the Agent Skills standard. A sequenced prompt set for Claude Code (Opus 4.8).**

This set lays the **foundation only** — the runtime, the multi-LLM model layer, the Mnesis knowledge connection, the Agent Skills subsystem, the base agent, the three agent-category abstractions, triggers/orchestration, governance, and the integration with current Mnesis. **It does not build concrete agents.** The three families that will live on this foundation — **writing agents** (ingest from email/chat/notes/docs), **action agents** (reminders, pre-meeting notes), and **maintenance agents** (the continuous "dream-cycle" curation of the knowledge + graph) — are scaffolded here as abstractions and trigger interfaces, and implemented in later, separate prompt sets.

Run after the Docker playbook (it uses the Mnesis MCP HTTP endpoint).

> **Relationship to the earlier bespoke agent set.** This LangChain-based foundation **supersedes** the hand-rolled loop/provider/MCP-client approach. Its principles carry over unchanged: agents reach Mnesis **only via MCP**; per-write **governance stays in Mnesis**; writes are **propose-or-apply**; runs are **budgeted and audited**. They are simply re-expressed on LangGraph + LangChain + langchain-mcp-adapters.

---

## Architecture decisions (read first)

1. **LangGraph is the orchestration layer; LangChain chat models give multi-LLM.** Agents are LangGraph graphs. Models are created through a single provider-agnostic factory (LangChain `init_chat_model` / provider classes) so the layer runs against OpenAI, Anthropic, Google, Mistral, Bedrock, Ollama, or any OpenAI-compatible endpoint by configuration — not code change. **Mnesis itself** is broadened to the same factory (F7), so "works with a variety of LLMs" holds for the whole system, not just the agents.
2. **Agents reach Mnesis only through MCP.** The Mnesis MCP server (D2) is connected via **langchain-mcp-adapters** and its `mnesis_*` tools become LangChain tools. The agentic package never imports the `mnesis` package. Mnesis's governance — redaction on ingest, the contradiction review queue, the lifecycle — therefore remains a guardrail the agents cannot bypass.
3. **Agent Skills (agentskills.io) is the capability-packaging standard.** Capabilities are `SKILL.md` folders loaded by progressive disclosure (discover name+description → activate full instructions on match → execute, optionally running bundled scripts / reading references). Every agent built on this foundation supports loading and using skills. (This is the same format Claude Code uses.)
4. **Governance lives in Mnesis; the agent layer adds budgets, allowlists, audit, and human-in-the-loop.** LangGraph checkpointers give durable state; LangGraph interrupts give approval gates for risky actions/writes.
5. **Three categories are abstractions now, concrete agents later.** WritingAgent / ActionAgent / MaintenanceAgent base classes and their trigger interfaces (event/source, schedule) are scaffolded; this set ships no concrete agents or source connectors beyond trivial smoke examples that prove the wiring.

---

## Tech stack

| Concern | Choice |
|---|---|
| Orchestration | **LangGraph** (graphs, checkpointers, interrupts) |
| Agent construction | LangChain `create_agent` / LangGraph prebuilt (verify the current API) |
| Multi-LLM models | LangChain chat models via a provider-agnostic factory (`init_chat_model`); provider extras: langchain-openai, -anthropic, -google-genai, -mistralai, -aws, -ollama, OpenAI-compatible |
| Knowledge connection | **langchain-mcp-adapters** (`MultiServerMCPClient`) → Mnesis MCP tools |
| Capabilities | **Agent Skills** (`SKILL.md` folders, progressive disclosure) per agentskills.io |
| Persistence | LangGraph checkpointer (SQLite/Postgres backends) |
| Observability | LangSmith tracing, **opt-in** |
| Offline tests | A deterministic fake chat model + a fake MCP tool source — no network, no keys |

LangChain/LangGraph move fast: every prompt instructs Claude Code to **verify the installed package APIs** (e.g. `create_agent` vs `create_react_agent`, `MultiServerMCPClient`, `init_chat_model`, checkpointer + interrupt APIs) before coding.

---

## Picking up the seams

| Seam (as built) | The foundation uses it as |
|---|---|
| D2 Mnesis MCP HTTP endpoint + token | The knowledge connection (via langchain-mcp-adapters). |
| The `mnesis_*` MCP tools | The agents' Mnesis toolset. |
| D4 provider switch / Mnesis `llm.py` | Broadened into the shared multi-LLM model factory (F7). |
| Ingestion plan/apply + review queue | The governance writing/maintenance agents flow through. |
| Mnesis decay / graph-lint / consolidation commands | What maintenance ("dream-cycle") agents will orchestrate later. |
| `SKILL.md` format (already used by Claude Code/Mnesis docs) | The Agent Skills the agents load. |

---

## Scope boundary

**In scope:** the agentic package on LangGraph; the multi-LLM model factory; the Mnesis MCP tool connection; the Agent Skills subsystem; the base agent + the three category abstractions; triggers/orchestration interfaces; governance/persistence/observability; the Mnesis multi-LLM integration; a Compose service scaffold; smoke tests proving the wiring.

**Deliberately out of scope (future sets):** concrete writing/action/maintenance agents · concrete source connectors (email/chat/notes/docs) · concrete skills beyond one example · backing the Web UI chat with an agent · multi-agent coordination (Phase 6).

---

## Reusing the standard template & rules

Same six-part template — **CONTEXT / OBJECTIVE / BUILD / CONSTRAINTS / ACCEPTANCE / ON DONE** — and standing rules: offline-testable (fake model + fake MCP tools, no keys/network); conventional commits; self-checking acceptance; keep `CLAUDE.md`/README in sync; verify installed LangChain/LangGraph/mcp APIs before coding. Keep **Opus 4.8** active. Prompts use the **F** prefix.

---

# The Prompts

---

## Prompt F1 — Agentic foundation scaffold + multi-LLM model factory

```
CONTEXT: Begin the agentic layer of Mnesis on the LangChain/LangGraph stack. It must be multi-LLM from the start and reach Mnesis only via MCP (later prompt). This step creates the package, dependencies, config, and the provider-agnostic model factory.

OBJECTIVE: Scaffold the mnesis_agents package on LangGraph, with a provider-agnostic chat-model factory and a deterministic offline model for tests.

BUILD:
- New package src/mnesis_agents/ (importable as mnesis_agents.*), pyproject with core deps langgraph, langchain, langchain-core, and OPTIONAL provider extras (langchain-openai, langchain-anthropic, langchain-google-genai, langchain-mistralai, langchain-aws, langchain-ollama) grouped so a minimal install works; langchain-mcp-adapters and langsmith declared for later prompts. Console script `mnesis-agents`.
- config.py: provider/model config from env — MNESIS_LLM_PROVIDER (openai|anthropic|google|mistral|bedrock|ollama|openai_compatible), MNESIS_LLM_MODEL, base_url/api-key passthrough, temperature, plus MNESIS_MCP_URL/MNESIS_MCP_TOKEN (used later) and MNESIS_AGENTS_STUB.
- models.py: get_chat_model() -> a LangChain BaseChatModel built via init_chat_model (or the matching provider class) from config; raise a clear error if the selected provider's extra isn't installed. Verify the installed init_chat_model / provider APIs first.
- A deterministic offline model: when MNESIS_AGENTS_STUB=1, return a fake BaseChatModel (e.g. a scripted/fake chat model) that yields canned responses/tool-calls so the whole layer is testable with no keys and no network.

CONSTRAINTS:
- Provider differences live only in the factory; the rest of the layer depends on BaseChatModel.
- Minimal install must import and run the stub without any provider extra present.
- Do NOT import the mnesis package.

ACCEPTANCE:
- tests/test_models.py (stub): get_chat_model() returns a usable model in stub mode with no keys; selecting a provider whose extra is missing raises a clear, actionable error; config parses env with sane defaults. `pytest -q` green; `pip install -e .` (core only) works.

ON DONE: run tests, commit ("feat(agents): foundation scaffold and multi-LLM model factory"), report the supported providers and the stub switch.
```

---

## Prompt F2 — Knowledge connection: Mnesis MCP tools as LangChain tools

```
CONTEXT: Agents use Mnesis as memory, reached ONLY through its MCP HTTP endpoint (D2). Wire that connection with langchain-mcp-adapters so the mnesis_* tools become LangChain tools.

OBJECTIVE: Implement the Mnesis tool source: connect to the MCP endpoint, load tools, and expose them through a registry, with an offline fake.

BUILD:
- knowledge.py: using langchain-mcp-adapters (verify MultiServerMCPClient / load_mcp_tools API), connect to MNESIS_MCP_URL with the bearer token and load the mnesis_* tools as LangChain tools. Support multiple MCP servers via config so other MCP tool sources can be added later.
- A ToolRegistry that aggregates tool sources (Mnesis MCP + later local tools + skills-as-tools) into one list for agents, with names namespaced to avoid collisions.
- Offline: a FakeMnesisTools source implementing the same interface with deterministic mnesis_query/mnesis_get/mnesis_ingest/mnesis_file_back/mnesis_impact stand-ins, so agents are testable without a running Mnesis.

CONSTRAINTS:
- The package reaches Mnesis ONLY via these MCP tools — never import mnesis internals.
- Connection/auth failures surface clearly; a missing Mnesis endpoint degrades to a clear error (or the fake source in tests), never a crash loop.
- Tests run with the fake source, no network.

ACCEPTANCE:
- tests/test_knowledge.py (fake source): the registry exposes the mnesis_* tools as LangChain tools with correct names/schemas; a tool invocation routes through and returns a result; namespacing avoids collisions; the real MultiServerMCPClient path is unit-tested with mocked transport. `pytest -q` green.

ON DONE: run tests, commit ("feat(agents): mnesis mcp tools via langchain-mcp-adapters"), report the loaded tool names.
```

---

## Prompt F3 — Agent Skills subsystem (agentskills.io standard)

```
CONTEXT: Every agent on this foundation must support the Agent Skills standard (agentskills.io): SKILL.md folders loaded by progressive disclosure. This is the same SKILL.md format Claude Code uses.

OBJECTIVE: Implement a conformant skills subsystem — discovery, activation, execution — and expose skills to agents.

BUILD:
- skills/loader.py implementing the three-stage progressive disclosure per the agentskills.io specification:
    * Discovery: scan configured skill directories (e.g. ./skills and a packaged dir) for skill folders, parse each SKILL.md YAML frontmatter (name + description REQUIRED; tolerate optional fields per the spec such as version/license/allowed-tools), and register a lightweight SkillCard{name, description, path}. Load ONLY name+description at this stage.
    * Activation: load_skill(name) reads the full SKILL.md instructions (and resolves referenced files) into context on demand.
    * Execution: a safe way to run a skill's bundled scripts/ and read its references/ and assets/ when the instructions call for it.
- A SkillRegistry the base agent can consume: it can list skill cards (for the system prompt / tool description) and activate a skill on demand. Decide and document the exposure mechanism (skills surfaced to the model as a "use_skill(name)" tool that activates + injects instructions, and/or as selectable system-prompt context) — keep it standard-compliant and model-agnostic.
- Ship ONE example skill folder (a trivial, clearly-marked sample, e.g. a "summarize-source" skill) to validate discovery/activation end to end. Do not build real domain skills here.
- Follow the agentskills.io spec for the SKILL.md schema; cite it in a comment.

CONSTRAINTS:
- Progressive disclosure must hold: discovery loads metadata only; full instructions load on activation.
- Skill execution of bundled scripts must be bounded/guarded (no arbitrary unbounded execution); document the safety posture.
- Model-agnostic: skills work regardless of the configured LLM provider.

ACCEPTANCE:
- tests/test_skills.py: discovery registers the example skill from name+description without reading the full body; activation loads the full instructions; a referenced file resolves; an invalid SKILL.md (missing name/description) is reported, not crashed. `pytest -q` green.

ON DONE: run tests, commit ("feat(agents): agent skills subsystem (progressive disclosure)"), report the skill schema fields honored and the exposure mechanism.
```

---

## Prompt F4 — Base agent + the three category abstractions

```
CONTEXT: With models (F1), Mnesis tools (F2), and skills (F3), build the reusable base agent on LangGraph and the abstract bases for the three agent categories. No concrete agents.

OBJECTIVE: Implement a base agent graph wiring model + tools + skills + memory + guardrail hooks, then the WritingAgent / ActionAgent / MaintenanceAgent abstractions with their trigger and policy shapes.

BUILD:
- base.py: build_agent(profile) -> a compiled LangGraph agent (use the current LangChain create_agent / LangGraph prebuilt API — verify it) wiring: the chat model (F1), the tool registry (F2 Mnesis tools + any local tools), the skills registry (F3), a checkpointer hook (F6 will configure it), and a profile (system prompt, tool/skill allowlist, write policy, budgets). Expose a run(input) / astream interface and return a structured result (final output, steps, tools/skills used, writes).
- categories/ with three ABCs over the base, each declaring its contract — NOT concrete agents:
    * WritingAgent: triggered by an inbound source event; parses an input artifact and ingests into Mnesis (via mnesis_ingest). Declares: trigger=event/source, write policy=ingest (Mnesis governs redaction/contradictions), expected input shape.
    * ActionAgent: triggered by event or schedule; reads Mnesis, reasons, and performs an external action via an action tool; write policy=propose-or-approved; declares an output/action channel interface (left abstract).
    * MaintenanceAgent: triggered by schedule (the "dream cycle"); reads+curates Mnesis and the graph (e.g. orchestrating decay/graph-lint/consolidation via Mnesis tools), proposing changes under governance; declares cadence + scope.
- A trivial example subclass per category (a no-op/echo "smoke" agent) ONLY to prove the base wiring runs end to end in stub mode. Mark them clearly as scaffolding, not real agents.

CONSTRAINTS:
- The base is provider- and tool-source-agnostic; categories add only their trigger + policy shape.
- No concrete production agents, no concrete source connectors.
- All Mnesis access via the F2 tools.

ACCEPTANCE:
- tests/test_base_agent.py (stub model + fake Mnesis tools + example skill): a smoke agent built via build_agent runs a turn, can call a Mnesis tool and activate the example skill, and returns the structured result; each category ABC enforces its required members (instantiating an incomplete subclass fails clearly). `pytest -q` green.

ON DONE: run tests, commit ("feat(agents): base agent and writing/action/maintenance abstractions"), report each category's trigger + write policy.
```

---

## Prompt F5 — Triggers & orchestration scaffold

```
CONTEXT: The three categories need ways to be fired: source/events (writing), schedules (maintenance dream-cycle, action reminders). Scaffold the trigger and orchestration mechanisms - interfaces and a runner - with NO concrete connectors or agents.

OBJECTIVE: Implement the trigger interfaces, an agent registry, and a runner/dispatcher that maps triggers to agents.

BUILD:
- triggers/: 
    * an EventTrigger / SourceConnector interface for inbound events (the shape a future email/chat/notes/docs connector implements -> emits a normalized InboundEvent{source, kind, payload, metadata}). Provide an in-memory/queue reference implementation for tests; no real connectors.
    * a ScheduleTrigger interface (cron/interval) for periodic firing (maintenance, reminders). Provide a simple scheduler (APScheduler or asyncio-based) wrapper.
- registry.py: an AgentRegistry where agents register with the trigger(s) they subscribe to.
- runner.py: a Runner that wires triggers -> registry -> agent execution: it consumes events / fires schedules and dispatches to the subscribed agents, applying per-run governance (F6). Resilient (one failing run does not stop the runner) and observable (emits run records).
- An entry point: `mnesis-agents run` starts the runner with whatever agents are registered (zero in this set = a healthy idle runner).

CONSTRAINTS:
- Interfaces + reference/in-memory implementations only; no concrete source connectors (email, etc.) and no concrete agents.
- The runner must be resilient and idempotent-friendly (a future connector can mark events processed).
- Triggering is decoupled from agent logic via the registry.

ACCEPTANCE:
- tests/test_runner.py (stub): registering a smoke agent against an in-memory event trigger and a fast schedule causes it to run on an emitted event and on a tick; a raising agent is caught and logged without stopping the runner; an idle runner (no agents) starts and stops cleanly. `pytest -q` green.

ON DONE: run tests, commit ("feat(agents): triggers, registry, and runner scaffold"), report the trigger interfaces and the dispatch flow.
```

---

## Prompt F6 — Governance, persistence, observability

```
CONTEXT: Consolidate the cross-cutting concerns the agents depend on: guardrails, durable state, human-in-the-loop, audit, and optional tracing - all wired into the base agent and runner.

OBJECTIVE: Add budgets/allowlists/write-policy enforcement, a LangGraph checkpointer, approval interrupts, an audit log, and opt-in LangSmith tracing.

BUILD:
- Guardrails: before any tool/skill dispatch, enforce the active profile's allowlist (refuse out-of-list, returned as a tool error) and budgets (max tool calls / tokens / wall-clock). Write-policy enforcement: propose vs apply; risky writes (e.g. supersession) require approval. Document that per-write safety (redaction, contradiction review) remains enforced by Mnesis itself.
- Persistence: configure a LangGraph checkpointer (SQLite default, Postgres optional) so agent state/threads are durable and resumable. Verify the current checkpointer API.
- Human-in-the-loop: use LangGraph interrupts so an agent can pause for approval (e.g. an action agent before sending, a writing agent before a supersede) and resume. Provide a simple approval interface for tests.
- Audit: an append-only run log (JSONL) capturing run id, category, trigger, profile, steps (tool/skill, status), writes, interrupts/approvals, stop reason, usage - never logging secrets/PII or full payloads.
- Observability: optional LangSmith tracing, enabled only when its env is set; off by default.

CONSTRAINTS:
- Guardrails fail closed: out-of-allowlist/out-of-budget actions are refused before any side effect.
- The audit log never contains secrets/PII; log statuses and ids.
- Tracing is strictly opt-in; default runs send nothing externally.

ACCEPTANCE:
- tests/test_governance.py (stub): an out-of-allowlist tool is refused; a budget cap stops a run with a flag; an interrupt pauses a run and a supplied approval resumes it; the checkpointer persists and resumes a thread; the audit log has one record per step with no leaked values; tracing stays off unless its env is set. `pytest -q` green.

ON DONE: run tests, commit ("feat(agents): governance, checkpointing, interrupts, audit"), report the enforcement points and the checkpointer backend.
```

---

## Prompt F7 — Mnesis integration: multi-LLM core, Compose, finalize

```
CONTEXT: Tie the foundation to Mnesis: broaden Mnesis's own LLM usage to the shared multi-LLM factory (so the whole system is provider-agnostic), wire a runtime service into Compose, and finalize docs. Still no concrete agents.

OBJECTIVE: Make Mnesis multi-LLM via the shared factory, add a profile-gated agentic-runtime Compose service, and document the foundation.

BUILD:
- Multi-LLM for Mnesis core: refactor Mnesis's llm.py to obtain its chat model from the shared provider-agnostic factory (reuse mnesis_agents.models or extract it to a small shared module both import) so Mnesis extraction/classification/synthesis run on any configured provider. PRESERVE existing behaviour and the stub; existing Mnesis tests must still pass. This broadens, not rewrites, the provider support.
- Shared config/env: a single, documented set of provider/model env vars used by both Mnesis and the agentic layer.
- Compose: a profile-gated service mnesis-agents-runtime running `mnesis-agents run`, depends_on mnesis (healthy), env MNESIS_MCP_URL=http://mnesis:8080 + token, the checkpointer/audit volume mounted; under the local-llm profile it targets the local model so the whole stack (Mnesis + agents + model) stays on-prem. With zero agents registered it runs healthy/idle.
- Docs: README "Agentic layer (LangChain foundation)" - the stack, multi-LLM config, the MCP connection, the Agent Skills support, and that concrete agents come later. CLAUDE.md: note the LangGraph foundation, the MCP-only boundary, the Agent Skills conformance, and that Mnesis is now provider-agnostic.
- A smoke/integration test proving the wiring end to end in stub mode: build a smoke agent, connect the (fake) Mnesis tools, activate the example skill, run via the runner under governance, and assert a clean result + audit record.

CONSTRAINTS:
- No Mnesis behaviour regression; its stub and existing tests stay green.
- The runtime reaches Mnesis only over MCP; the local-llm profile makes no external inference calls.
- No concrete agents - the runtime is an idle, healthy host awaiting later agent sets.

ACCEPTANCE:
- `pytest -q` across both packages green (Mnesis unchanged behaviourally, now provider-configurable). `docker compose --profile agents up -d` starts mnesis + an idle healthy mnesis-agents-runtime. Switching MNESIS_LLM_PROVIDER changes the model used by both Mnesis and the agent layer with no code change. The end-to-end smoke test passes.

ON DONE: commit ("feat(agents): mnesis multi-llm integration, compose runtime, docs"), report the shared env vars and the idle-runtime bring-up.
```

---

## Verifying the foundation (after F7)

1. `pytest -q` across both packages — green, fully offline (fake model + fake Mnesis tools + example skill).
2. **Multi-LLM:** set `MNESIS_LLM_PROVIDER` to two different providers — both Mnesis and the agent layer pick up the change with no code edit; a missing provider extra errors clearly.
3. **Knowledge connection:** the registry exposes the `mnesis_*` tools as LangChain tools; against a running Mnesis they invoke for real, and the agentic package never imports `mnesis`.
4. **Agent Skills:** the example skill is discovered by name+description, activated on demand (full instructions load only then), and its referenced files resolve — progressive disclosure holds.
5. **Base + categories:** a smoke agent runs through the runner, calls a Mnesis tool, activates the skill, and returns a structured result; the three category ABCs enforce their contracts.
6. **Governance:** out-of-allowlist/out-of-budget actions fail closed; an interrupt pauses and resumes; the checkpointer persists a thread; the audit log leaks nothing.
7. **Runtime:** `docker compose --profile agents up -d` brings up an idle, healthy agent runtime reaching Mnesis over MCP; `--profile local-llm` keeps inference on-prem.

If all seven hold, the agentic layer has a real, open-source, multi-LLM, skills-conformant foundation — and the writing, action, and maintenance agents become later sets that just register against it.

---

## Notes for running with Claude Code

- Run F1 → F7 in order on Opus 4.8, after the Docker playbook. Most prompts are pytest-testable; F7 wires Compose.
- Verify the **current LangChain/LangGraph/langchain-mcp-adapters APIs** before each relevant prompt (`create_agent` vs prebuilt, `init_chat_model`, `MultiServerMCPClient`, checkpointer + interrupt). These packages change quickly; the prompts name capabilities, not frozen signatures.
- The boundary to enforce in review: **the agentic package imports nothing from `mnesis`** — Mnesis is reached only via the MCP tools, which keeps its governance unbypassable. And **Agent Skills progressive disclosure must hold** — discovery loads metadata only.
- Everything stays testable offline via the fake chat model + fake Mnesis tools; no prompt should require keys, a network, or a running Mnesis to pass its tests.
- This set deliberately ships **no concrete agents**. Writing, action, and maintenance agents — and their real source connectors and domain skills — are separate prompt sets built on this foundation.
```
