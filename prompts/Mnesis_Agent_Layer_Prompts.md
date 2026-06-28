# Mnesis — Agent Layer Build Playbook

**A runtime agent that reasons and acts using Mnesis as its memory. A sequenced prompt set for Claude Code (Opus 4.8).**

So far Mnesis is the memory and its surfaces (CLI, MCP for agents, the Web UI for humans). This set builds the first component that *thinks and acts on top of it*: a Python agent runtime with an agentic tool-use loop, connected to Mnesis over the MCP HTTP endpoint, that retrieves from and writes back to Mnesis. One core powers three archetypes — an **assistant** (grounded, interactive Q&A), a **research agent** (bounded multi-step investigation that crystallizes findings), and an **ingestion daemon** (autonomous, watches a source and feeds Mnesis). It runs against Anthropic *or* a local model, so an entire deployment — agent, Mnesis, and model — can sit inside one trust boundary.

Run after the Docker playbook (it uses the D2 MCP HTTP endpoint and the D4 provider switch).

---

## Architecture decisions (read first)

1. **The agent reaches Mnesis only through the MCP client.** It never imports Mnesis internals. Memory is reached via the MCP HTTP endpoint (D2) using the `mnesis_*` tools. This keeps the agent a clean, separately-deployable client, and means **Mnesis's own governance — redaction on ingest, the contradiction review queue, the lifecycle — is the guardrail the agent cannot bypass.**
2. **The agent owns its loop and tool dispatch.** It does not rely on a hosted/server-side MCP connector. It lists MCP tools, presents them to the model as tool-use definitions, runs the loop, and dispatches tool calls itself. That is what makes the provider switch real: the same loop runs against the Anthropic Messages API or a local OpenAI-compatible/Ollama model.
3. **One core, per-archetype profiles.** A single agent core (loop + MCP client + provider) is parameterized by a profile: system prompt, tool allowlist, autonomy/budget, write policy, entry mode. Assistant / research / daemon are profiles, not separate programs.
4. **Governance lives in Mnesis; the agent adds budgets and allowlists.** Per-write policy (redaction, supersession-needs-review) is enforced by Mnesis. The agent layer adds *which tools an archetype may call*, *budgets* (max tool calls / tokens / wall-clock), and an *audit log* of every step. The agent never gets a way around Mnesis's review queue.
5. **The compounding loop closes here.** Agents both consume Mnesis (retrieve, ground, cite) and contribute to it (file-back digests, ingest) — explorations become a source, exactly as the architecture intends.

---

## Tech stack

Python. **Anthropic Messages API** with tool-use, and a **local OpenAI-compatible / Ollama** provider via the existing `MNESIS_LLM_PROVIDER` switch (D4) — the agent loop is provider-agnostic. The **official `mcp` SDK (client side)** connects to the Mnesis MCP HTTP endpoint with the bearer token. A CLI for the interactive archetypes; an optional profile-gated Compose service for the daemon. Everything is testable offline with a stub provider and a fake in-process tool source — no network, no running Mnesis required for tests.

---

## Picking up the seams

| Seam (as built) | The agent layer uses it as |
|---|---|
| D2 MCP HTTP endpoint + `MNESIS_MCP_TOKEN` | The agent's connection to memory. |
| D4 provider switch (`MNESIS_LLM_PROVIDER`) | Tool-use across Anthropic and a local model. |
| The `mnesis_*` MCP tools (query/get/entity/impact/ingest/file_back/…) | The agent's tool registry. |
| Ingestion plan/apply + the contradiction review queue | The governance the daemon's writes flow through. |
| "Your explorations are a source" (architecture) | Crystallization: the agent files findings back. |

---

## Scope boundary

**In scope:** the agent core (loop, MCP client, provider tool-use) · grounding + crystallization + session hooks · the three archetypes · budgets/allowlists/audit · a pluggable local-tool registry (seam) · a daemon Compose service.

**Deliberately deferred:** external tools beyond the registry seam (web search/fetch ship off by default) · backing the Web UI chat (G5) with the agent (a clean later swap — noted as a seam) · multi-agent coordination (Phase 6) · advanced planner/reflection beyond a budgeted tool-use loop.

---

## Reusing the standard template & rules

Same six-part template — **CONTEXT / OBJECTIVE / BUILD / CONSTRAINTS / ACCEPTANCE / ON DONE** — and standing rules: offline with `MNESIS_LLM_STUB=1` and a fake tool source; conventional commits; self-checking acceptance; keep `CLAUDE.md`/README in sync. Keep **Opus 4.8** active throughout. Prompts use the **A** prefix.

---

# The Prompts

---

## Prompt A1 — Agent package & MCP client connection

```
CONTEXT: Build the foundation of a runtime agent that uses Mnesis as memory. The agent must reach Mnesis ONLY through its MCP HTTP endpoint (D2) - never by importing Mnesis internals - so it stays a separately-deployable client.

OBJECTIVE: Scaffold the agent package and an MCP client that connects to the Mnesis MCP endpoint, lists its tools, and exposes them as a normalized tool registry, with an offline fake tool source for tests.

BUILD:
- New package src/mnesis_agent/ (importable as mnesis_agent.*), its own pyproject entry, console script `mnesis-agent`.
- config.py: MNESIS_MCP_URL, MNESIS_MCP_TOKEN, plus reuse of the LLM provider/model/stub env from the existing stack.
- mcp_client.py: connect to the Mnesis MCP HTTP endpoint (verify the installed `mcp` SDK client API and match it), authenticate with the bearer token, list tools, and expose: list_tools() -> [ToolSpec{name, description, input_schema}] and call_tool(name, args) -> result. Handle connection/auth errors with clear messages.
- A ToolSource abstraction so the registry is decoupled from MCP specifically; provide a FakeToolSource (in-process, deterministic) implementing the same interface for offline tests (e.g. canned mnesis_query/mnesis_get/mnesis_ingest results).
- registry.py: a ToolRegistry aggregating one or more ToolSources into a single normalized tool list + a dispatch(name, args) that routes to the owning source.

CONSTRAINTS:
- The agent package must NOT import the mnesis package; Mnesis is reached only via the MCP client / ToolSource interface.
- Tests run with no network and no running Mnesis, using FakeToolSource.

ACCEPTANCE:
- tests/test_mcp_client.py + test_registry.py (fake source): list_tools returns normalized specs; dispatch routes a call to the right source and returns its result; an auth/connection error surfaces clearly. `pytest -q` green.

ON DONE: run tests, commit ("feat(agent): mcp client and tool registry"), report the ToolSpec shape and how the fake source stands in for Mnesis.
```

---

## Prompt A2 — Provider tool-use (Anthropic + local)

```
CONTEXT: The agent loop must call an LLM with tools and handle tool-use round-trips, across both Anthropic and a local model, reusing the D4 provider switch. This is what makes the agent provider-agnostic and on-prem capable.

OBJECTIVE: Implement a unified tool-use completion in src/mnesis_agent/provider.py over Anthropic and a local OpenAI-compatible/Ollama backend, with a deterministic stub.

BUILD:
- A normalized request: complete_with_tools(system, messages, tools, ...) -> AssistantTurn{text, tool_calls:[{id,name,args}], stop_reason}. Messages and tool results use a provider-neutral internal representation; the provider adapts to/from it.
- Anthropic adapter: map ToolSpec -> Anthropic tools (name/description/input_schema), send via the Messages API, parse tool_use blocks into tool_calls, and format tool results as tool_result blocks on the next call. Verify the installed anthropic SDK's tool-use API.
- Local adapter (MNESIS_LLM_PROVIDER=local): map tools to the OpenAI-compatible/Ollama function-calling format, parse tool_calls back. Same neutral interface.
- Stub (MNESIS_LLM_STUB=1): a deterministic provider that emits a SCRIPTED sequence of tool_calls then a final answer, driven by fixture markers, so the whole agent loop is testable offline with no network.
- Centralize model + token/temperature config; expose token usage for budgeting (A6).

CONSTRAINTS:
- One neutral interface; provider differences stay inside the adapters.
- Default provider/behaviour unchanged from the rest of the stack; the stub needs no key and no network.

ACCEPTANCE:
- tests/test_provider.py: with the stub, complete_with_tools returns scripted tool_calls then a final answer; a tool result fed back produces the next turn; the Anthropic and local adapters' schema mapping is unit-tested with mocked transport (tool_use round-trip shapes correct). `pytest -q` green.

ON DONE: run tests, commit ("feat(agent): provider tool-use across anthropic and local"), report the neutral interface.
```

---

## Prompt A3 — The agent loop (core runtime)

```
CONTEXT: With a tool registry (A1) and provider tool-use (A2), build the agentic loop: reason -> call tools -> observe -> repeat -> answer, with hard guardrails so it can never run away.

OBJECTIVE: Implement src/mnesis_agent/loop.py: a bounded tool-use loop with budgets, error recovery, and a structured result + transcript.

BUILD:
- run_agent(profile, input, tools, provider) -> AgentResult: assemble system (from profile) + messages; call provider.complete_with_tools; while stop_reason is tool_use, dispatch each tool_call via the registry, append tool_result (including tool errors as results so the model can recover), and loop; stop on a final answer or a guardrail.
- Guardrails: max_iterations, max_tool_calls, token budget (from provider usage), and a wall-clock deadline; a no-progress detector (e.g. repeated identical tool calls). On any limit, stop gracefully and return what it has, flagged.
- AgentResult: {final_text, transcript (every turn + tool call/result), tools_used, citations (page ids referenced), writes (any write-tool calls performed), stop_reason, usage}. 
- A step audit hook: emit each step (thought summary, tool, args-redacted, result-status) to a run log (A6 will persist it).

CONSTRAINTS:
- Tool errors NEVER crash the loop - they return to the model as results.
- Every limit has a defined, safe stop; the loop cannot iterate unbounded.
- Pure orchestration: no Mnesis-specific logic here (that's A4); the loop is tool-source agnostic.

ACCEPTANCE:
- tests/test_loop.py (stub provider + fake tools): a scripted multi-step run reaches a final answer with a populated transcript and tools_used; a tight max_iterations stops a runaway and flags it; a tool that errors is surfaced to the model and the run recovers; a repeated-call no-progress case is caught. `pytest -q` green.

ON DONE: run tests, commit ("feat(agent): bounded tool-use loop"), report the guardrails and AgentResult shape.
```

---

## Prompt A4 — Memory integration: grounding, crystallization, session hooks

```
CONTEXT: Make the loop Mnesis-aware: load relevant context at the start, ground answers in retrieved pages with citations, and (when policy allows) crystallize results back into Mnesis - closing the compounding loop at the agent level. All via the MCP tools, never Mnesis internals.

OBJECTIVE: Add the memory behaviours around the core loop: session-start context loading, citation tracking, and governed write-back.

BUILD:
- Session start: before the main loop, run a context-load step (mnesis_query on the goal, optionally mnesis_entity/impact for named entities) and inject the top results into the system/context so the agent starts grounded. Bounded.
- Grounding + citations: a system-prompt convention that the agent answers from retrieved Mnesis pages and cites page ids; the loop already captures citations (A3) - ensure they map to real pages returned by tools.
- Crystallization (write-back), governed by a write policy:
    * "propose" mode: the agent proposes a write (a digest {question, answer, sources}) and returns it as a proposal in AgentResult WITHOUT calling a write tool.
    * "apply" mode: the agent may call mnesis_file_back / mnesis_ingest within its allowlist and budget. Per-write governance (redaction, contradiction review) is enforced by Mnesis - the agent just calls the tool.
- Session end: optionally crystallize the session per policy.

CONSTRAINTS:
- All memory access is through MCP tools; no direct Mnesis calls.
- Write-back obeys the profile's write policy and allowlist (enforced in A6); in propose mode nothing is written.
- Citations must reference pages actually returned by tools - no invented ids.

ACCEPTANCE:
- tests/test_memory.py (stub + fake Mnesis tools): a run loads context from mnesis_query and produces a cited answer whose citations exist; in apply mode it calls mnesis_file_back and AgentResult.writes records it; in propose mode the same scenario writes nothing and returns a proposal. `pytest -q` green.

ON DONE: run tests, commit ("feat(agent): mnesis grounding and crystallization"), report the propose-vs-apply behaviour.
```

---

## Prompt A5 — Archetypes: assistant, research, ingestion daemon

```
CONTEXT: One core, three profiles. Build the archetypes as profiles over the loop + memory behaviours, each with its own system prompt, tool allowlist, autonomy, and entry mode.

OBJECTIVE: Implement the three profiles and their entry points (CLI for interactive ones, a watcher for the daemon).

BUILD:
- Profiles in src/mnesis_agent/profiles/ - each = {system_prompt, tool_allowlist, write_policy, budgets, entry_mode}:
    * assistant: read tools (query/get/entity/impact/traverse); write policy = propose-only; interactive. CLI `mnesis-agent assistant` = a REPL: each user turn runs the loop, prints a grounded, cited answer, and surfaces any file-back proposal for the user to confirm (confirming then calls mnesis_file_back).
    * research: read tools + (optional) registered external tools + file_back; write policy = apply (digests only), bounded by budget; never supersede. CLI `mnesis-agent research "<goal>"` runs a bounded multi-step investigation (query, traverse, impact; synthesize), prints a cited report, and crystallizes a digest back into Mnesis; prints the created page id.
    * ingest-daemon: ingest tool (+ read for dedup); long-running; autonomy bounded by Mnesis's own ingest governance. CLI `mnesis-agent ingest-daemon --watch <path>` watches a directory; on a new file it runs a minimal ingest flow via mnesis_ingest. Contradictions/supersessions are handled by Mnesis (auto-resolved high-margin, queued low-margin) - the daemon does not force resolutions. Log each ingest outcome.
- Shared run plumbing: build the registry (MCP client to Mnesis + optional local tools), select provider, apply the profile, run.

CONSTRAINTS:
- An archetype can only call tools in its allowlist; attempts outside it are refused before dispatch (A6 enforces).
- The daemon must be resilient (one bad file doesn't kill it) and idempotent (re-seeing a file doesn't duplicate).
- Interactive archetypes keep the human in the loop for writes (assistant proposes; research's writes are digests only).

ACCEPTANCE:
- tests/test_archetypes.py (stub): assistant produces a cited answer and a (non-applied) file-back proposal; research completes within budget and files exactly one digest (visible via a fake mnesis_get); the daemon, fed a new file event, calls mnesis_ingest once and routes a fixture contradiction to review rather than forcing it; a malformed file is logged and skipped. `pytest -q` green.

ON DONE: run tests, commit ("feat(agent): assistant, research, and ingestion-daemon archetypes"), report each profile's allowlist and write policy.
```

---

## Prompt A6 — Guardrails, audit, and the external-tool registry

```
CONTEXT: Consolidate the safety story for an agent that can act and write, and add the seam for optional external tools.

OBJECTIVE: Enforce per-archetype allowlists and budgets, persist an append-only run audit, and provide a pluggable local-tool registry (off by default).

BUILD:
- Policy enforcement: a layer that, before every dispatch, checks the tool is in the active profile's allowlist (refuse + return an error-result otherwise) and that budgets (max tool calls/tokens/wall-clock) are not exceeded. Write tools are gated by the profile's write policy. Mnesis's own governance is relied upon for per-write safety (redaction, supersession review) - document that the agent cannot bypass it.
- Audit: an append-only run log (JSONL under a configurable dir) capturing run id, profile, input, each step (tool, args with secrets/PII never logged, result status), writes performed, stop reason, and usage. One record per step; never log redacted values.
- External-tool registry seam: a LocalToolSource interface so optional tools (e.g. a web-search/fetch tool) can be registered alongside the Mnesis MCP source. Ship NONE enabled by default; provide one example local tool behind an explicit config flag, disabled unless configured. Research is the only profile that may use them, and only if its allowlist includes them.

CONSTRAINTS:
- Out-of-allowlist or out-of-budget calls are refused deterministically, before any side effect.
- The audit log never contains secrets/PII or full tool payloads that could - log statuses and ids.
- Optional local tools are opt-in; a plain run starts with only the Mnesis tools.

ACCEPTANCE:
- tests/test_policy_audit.py: an out-of-allowlist tool call is refused and surfaced to the model; exceeding a budget stops the run with a flag; the audit log has one record per step with no leaked values; an example local tool is callable only when its flag is set and only for the research profile. `pytest -q` green.

ON DONE: run tests, commit ("feat(agent): policy, budgets, audit, and tool registry seam"), report the enforcement points.
```

---

## Prompt A7 — Dockerize the daemon, Compose wiring, finalize

```
CONTEXT: Ship the agent layer as part of the deployment - the daemon as a service - including the fully-local, data-isolated configuration.

OBJECTIVE: Containerize the agent, add a profile-gated daemon service to Compose pointing at the Mnesis MCP endpoint, support the local-model provider end to end, and finalize docs.

BUILD:
- agent/Dockerfile (or reuse the base image): installs mnesis_agent; entrypoint runs `mnesis-agent` with args.
- docker-compose.yml: profile-gated service mnesis-agent (the ingest-daemon) depends_on mnesis (healthy), env MNESIS_MCP_URL=http://mnesis:8080 + token, a mounted watch directory, restart unless-stopped. Under the existing local-llm profile, the agent targets the local model so inference stays on-prem (no external calls).
- Make targets: agent-assistant / agent-research (interactive, against a running stack) and the daemon under `--profile agent`.
- README "Agent layer" section: the three archetypes, run recipes, the MCP-endpoint connection, and the fully-local recipe (agent + Mnesis + local model inside one trust boundary). Note the seam: the assistant archetype can later back the Web UI chat endpoint (G5), upgrading simple RAG to agentic retrieval.
- CLAUDE.md: Mnesis now has agents that both consume and contribute to it, reaching memory only via MCP; governance remains enforced by Mnesis.

CONSTRAINTS:
- The daemon container reaches Mnesis only over the MCP endpoint on the internal network.
- The fully-local profile must perform NO external inference calls.
- Stateless agent container (run logs to a mounted/volume path if persisted); all knowledge state stays in Mnesis.

ACCEPTANCE:
- From a clean stack: `docker compose --profile agent up -d` starts mnesis + the daemon healthy; dropping a file in the watch dir ingests it into Mnesis (visible in the Web UI / via mnesis_query); `make agent-research "<goal>"` produces a cited report and files a digest; with `--profile local-llm` the same runs use the local model with no external inference.

ON DONE: commit ("feat(agent): dockerized daemon, compose wiring, docs"), report the run recipes and the fully-local bring-up.
```

---

## Verifying the agent layer (after A7)

1. `pytest -q` across the agent package — green, fully offline (stub provider + fake Mnesis tools).
2. **Assistant:** `mnesis-agent assistant` against a running Mnesis → ask about seeded content → a grounded, cited answer; it *proposes* filing a useful answer back, and only writes when you confirm.
3. **Research:** `mnesis-agent research "<goal>"` → a bounded multi-step run that queries and traverses Mnesis, prints a cited report, and crystallizes a digest you can then find in the Web UI — the compounding loop, driven by an agent.
4. **Daemon:** `docker compose --profile agent up -d` → drop a file in the watch dir → it's ingested into Mnesis; a conflicting source lands in the contradiction review queue rather than being force-resolved.
5. **On-prem:** add `--profile local-llm` → the same three archetypes run with the local model, making no external inference calls — agent, memory, and model all inside the trust boundary.
6. **Guardrails:** a budget cap stops a long run gracefully; an out-of-allowlist tool is refused; the run audit log records steps with no leaked secrets/PII.

If all six hold, Mnesis has a reasoning layer that uses it as memory and feeds it back — and because the agent only ever speaks MCP, the same agent works against any Mnesis instance, local or remote.

---

## Notes for running with Claude Code

- Run A1 → A7 in order on Opus 4.8, after the Docker playbook. A1–A4 and A6 are testable with pytest; A5 adds the archetypes; A7 deploys.
- The boundary to enforce in review: **the agent reaches Mnesis only through the MCP client.** If a diff imports the `mnesis` package into `mnesis_agent`, that is the bug — it collapses the clean client boundary and lets the agent sidestep Mnesis's governance.
- The safety judgement that matters: **the agent cannot bypass Mnesis's redaction or contradiction review, and its own budgets/allowlists must fail closed.** Out-of-policy or out-of-budget actions are refused before any side effect, never best-effort.
- Verify the installed `mcp` (client) and `anthropic` (tool-use) SDK APIs before A1/A2; the stub provider + fake tool source keep the whole layer testable without either a model or a running Mnesis.
- Web search and other external capabilities are a deliberate seam (A6), shipped off by default. Turning them on is a config flag for the research profile — keep them bounded and audited like every other tool.
```
