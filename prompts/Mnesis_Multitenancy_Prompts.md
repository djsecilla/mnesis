# Mnesis — Multitenancy Build Playbook

**Add hard multitenancy to Mnesis: many tenants, each with a fully isolated knowledge base, where one tenant can never reach another's data through any surface. A sequenced, security-first prompt set for Claude Code (Opus 4.8).**

This change reaches into Mnesis's core (the two-speed store, every surface, and the agent layer), so it is built **isolation-first**: the data layer is made tenant-scoped *by construction* before any auth or feature work, so that cross-tenant access is structurally impossible rather than merely filtered out. Claude Code starts at **T1**; the set is the full, ordered plan.

> **Tenancy model (assumption — adjust if needed).** A **tenant** is the hard isolation boundary; it can represent a single user *or* an organization. Within a tenant there are **principals** (human users and agents) with **roles**. Cross-tenant isolation is absolute; finer visibility (private vs shared) lives *within* a tenant. If you want strictly per-user tenants, that's the degenerate case (one principal per tenant) and works unchanged.

---

## Security stance (read first)

- **Isolation by construction, not by query discipline.** Each tenant has a physically separate store (its own Markdown+Git root and its own index/graph/state DBs under `tenants/<tenant_id>/`). The data layer is only reachable through a resolved `TenantContext`; there is no global/ambient store and no API that accepts a raw cross-tenant path.
- **Tenant comes only from the verified credential.** Never from a request field, header the client controls, path, or content. A request can't ask for another tenant's data. (Same principle as "recipients never from content" in the egress work.)
- **Fail closed.** If the tenant/principal can't be resolved, deny — never fall back to a default tenant or to "all".
- **Defense in depth.** Physical separation (primary) + a path-traversal guard that keeps every resolved path inside the tenant root + tenant-id assertions on any cache row that exists + fail-closed resolution + cross-tenant **negative tests** on every surface.
- **No cross-tenant features by default.** No cross-tenant search, graph, or sharing. If ever needed, it's a separate, explicit, audited mechanism — not a default.
- **The bar is the negative test.** Each surface must prove that tenant A — via crafted input, a forged tenant id, a traversal path, search, graph, MCP, the Web UI, or an agent — cannot retrieve tenant B's data.

---

## Architecture decisions

1. **Physical per-tenant stores.** `tenants/<tenant_id>/` holds the canonical `pages/`, `sources/`, and its **own git repo**; `tenants/<tenant_id>/.cache/` holds the rebuildable `index.db`, graph, and `state.db` (durable runtime state + review queue). Rebuild is per-tenant. Separate git per tenant also gives clean per-tenant history, export, and backup.
2. **`TenantContext` is mandatory and threaded from the surface boundary.** Stores, search, graph, ingest, and state are all constructed *from* a tenant context. A data-layer call without one is impossible to express.
3. **Credentials map to {tenant, principal, role}.** The single MCP bearer token is replaced by tenant-scoped credentials; the Web UI resolves tenant+principal from the session; the CLI resolves a tenant context for tenant ops and a separate admin surface for lifecycle.
4. **Within-tenant visibility is a second layer.** Pages/sources carry an owner + visibility (private vs tenant-shared); search/get/graph filter to what the principal may see. Cross-tenant is already impossible from (1).
5. **Backward compatible.** Existing single-store data migrates non-destructively into a `default` tenant; single-tenant deployments run transparently as one tenant.

---

## Scope boundary

**In scope:** tenant model + `TenantContext` + per-tenant physical stores · per-tenant caches/rebuild/graph/state · authentication + principal/tenant resolution · authorization + within-tenant visibility · enforcement across MCP, Web UI, CLI · per-tenant agent layer (writing/action/maintenance/egress) · tenant lifecycle/admin/quotas · migration + deploy + isolation verification.

**Deliberately out of scope:** cross-tenant sharing/search · SSO/identity-provider integration beyond a pluggable seam · per-tenant encryption-at-rest (noted as a hardening option) · billing/metering.

---

## Reusing the standard template & rules

Same six-part template — **CONTEXT / OBJECTIVE / BUILD / CONSTRAINTS / ACCEPTANCE / ON DONE** — and standing rules: offline-testable (seed two tenants in tests; no network); conventional commits; self-checking acceptance; keep `CLAUDE.md`/README in sync; verify installed APIs; **existing single-tenant behaviour must keep working via the `default` tenant**. Keep **Opus 4.8** active. Prompts use the **T** prefix. Throughout, **the cross-tenant negative test is the acceptance bar.**

---

# The Prompts

---

## Prompt T1 — Tenant model, TenantContext, per-tenant stores (the isolation primitive)

```
CONTEXT: Make Mnesis multitenant from the data layer up. Before any auth or feature work, the store must be tenant-scoped BY CONSTRUCTION so cross-tenant access is structurally impossible. This touches Mnesis core.

OBJECTIVE: Introduce a tenant model and a mandatory TenantContext, refactor the store to be built only from a tenant context against a per-tenant physical root, remove any global store, and migrate existing data into a `default` tenant non-destructively.

BUILD:
- A Tenant model (tenant_id, name, status, created) and a tenant registry (where tenants are recorded - a small metadata store outside any tenant root).
- TenantContext{tenant_id, root_path} resolved at boundaries (later prompts resolve it from credentials/sessions). The canonical store (pages/, sources/, the tenant's own git repo) lives at tenants/<tenant_id>/; rebuildable caches at tenants/<tenant_id>/.cache/.
- Refactor the store/repo/git layer so every store object is CONSTRUCTED from a TenantContext - there is no module-level/global store and no function that takes a raw cross-tenant path. A path-resolution guard asserts every resolved path stays within the tenant root (reject traversal / absolute escapes).
- Migration: a command that moves existing single-store data into tenants/default/ (canonical + re-inited git) idempotently and losslessly; single-tenant deployments then run as the one `default` tenant transparently.

CONSTRAINTS:
- No global/ambient store may remain; the store cannot be obtained without a TenantContext.
- Resolved paths can never escape the tenant root (path-traversal guard, fail closed).
- Migration is non-destructive and idempotent; existing behaviour is preserved under `default`.

ACCEPTANCE:
- tests/test_tenant_store.py: a store cannot be constructed without a TenantContext; two tenants write pages that land under separate roots; a crafted path/id attempting to escape a tenant root is refused; the migration moves existing data into `default` and a re-run is a no-op; existing single-tenant tests pass against `default`. `pytest -q` green.

ON DONE: commit ("feat(tenancy): tenant model, TenantContext, per-tenant stores, migration"), report the on-disk layout and the no-global-store guarantee.
```

---

## Prompt T2 — Per-tenant caches, rebuild, graph, state

```
CONTEXT: With per-tenant canonical stores (T1), the rebuildable caches and durable state must also be per-tenant, and all derived operations (search, graph, decay, review queue) must operate strictly within a tenant.

OBJECTIVE: Make the FTS index, graph backend, state.db (access counts + review queue), and rebuild per-tenant, scoped by TenantContext.

BUILD:
- Per-tenant cache: index.db (FTS5), the graph backend, and state.db live under tenants/<tenant_id>/.cache/ and are opened from the TenantContext. No shared cache across tenants.
- All derived ops take/return within the tenant: search/query, get, entity/neighbors/traverse/impact, graph stats, decay, and the review queue operate only on the tenant's data.
- Rebuild is per-tenant: rebuilding tenant A reconstructs A's caches from A's Markdown only, never touching B.
- If any cache table is ever shared for operational reasons, every row carries tenant_id and every read asserts it (defense in depth) - but prefer separate per-tenant DB files.

CONSTRAINTS:
- No cache, index, graph, or state is shared across tenants; each is opened from the TenantContext.
- Search/graph/traverse for tenant A can never surface tenant B's pages/entities/edges.
- Rebuild is tenant-scoped and cannot cross roots.

ACCEPTANCE:
- tests/test_tenant_cache.py (two seeded tenants A and B with overlapping topics): A's search/query/graph/traverse/impact return only A's data; rebuilding A leaves B untouched; A's review queue and access counts are independent of B's; there is no query that returns mixed-tenant results. `pytest -q` green.

ON DONE: commit ("feat(tenancy): per-tenant caches, rebuild, graph, state"), report the cache layout and the isolation tests.
```

---

## Prompt T3 — Authentication & principal/tenant resolution

```
CONTEXT: Replace the single global MCP token with credentials that map to a tenant and a principal, and resolve that context at every surface boundary - fail closed, never trust client-supplied tenant.

OBJECTIVE: A credential store mapping credentials -> {tenant_id, principal_id, role}, and a resolver that yields an authenticated (TenantContext, Principal) at boundaries.

BUILD:
- A credential store (outside any tenant root) issuing/validating tenant-scoped, principal-scoped credentials (e.g. opaque tokens / API keys; the Web UI may use sessions/JWT later). Each credential resolves to {tenant_id, principal_id, role in {admin, member, readonly, agent}}.
- resolve_principal(credential) -> (TenantContext, Principal) | deny. The tenant_id is taken ONLY from the validated credential - never from a request body/header/path/content. Absent/invalid/expired credentials are denied.
- A clear API for issuing credentials for a tenant+principal (used by the lifecycle/admin prompt T7) and revoking them.
- Secrets at rest handled safely (hashed/stored in a secret store), never logged.

CONSTRAINTS:
- Tenant identity derives solely from the verified credential; any client-supplied tenant id is ignored/refused.
- Fail closed: unresolved -> deny, no default tenant fallback.
- Credentials are never logged; validation is constant-time where applicable.

ACCEPTANCE:
- tests/test_auth.py: a tenant-A credential resolves to tenant A only; an invalid/absent/expired credential is denied; a request that includes a different tenant_id than the credential's is resolved to the CREDENTIAL's tenant (the supplied id is ignored), and a test asserts data access still scopes to A; revocation denies subsequently. `pytest -q` green.

ON DONE: commit ("feat(tenancy): credential store and principal/tenant resolution"), report the credential->context mapping and the fail-closed rule.
```

---

## Prompt T4 — Authorization & within-tenant data visibility

```
CONTEXT: Inside a resolved tenant, add authorization (what a principal may do) and a visibility model (what a principal may see). Cross-tenant is already impossible (T1/T2); this is the finer layer.

OBJECTIVE: Role-based authorization for reads/writes, and a private/tenant-shared visibility model enforced in get/search/graph/ingest.

BUILD:
- Authorization: role checks (admin/member can write; readonly cannot; agent principals get a scoped capability set). A single authorize(principal, action, resource) used by the surfaces.
- Visibility: pages/sources carry owner_principal + visibility in {private, shared} (shared = visible to all principals in the tenant; private = owner-only). Default visibility is configurable per tenant (default: shared within the tenant).
- Enforcement: get/search/query/graph/traverse filter to resources the principal may see (their own private + tenant-shared); ingest/file-back set owner + visibility; the graph and search never surface a node/page the principal cannot see.

CONSTRAINTS:
- Visibility filtering is applied in the data/query layer, not just the UI, so no surface can leak a private resource.
- Writes respect role; visibility changes respect ownership/role.
- Cross-tenant remains structurally impossible; this layer only narrows within a tenant.

ACCEPTANCE:
- tests/test_visibility.py (one tenant, two principals): a private page owned by P1 is invisible to P2 in search/get/graph; a shared page is visible to both; a readonly principal cannot write; P2 cannot read P1's private page via any query path. `pytest -q` green.

ON DONE: commit ("feat(tenancy): authorization and within-tenant visibility"), report the visibility scopes and the enforcement points.
```

---

## Prompt T5 — Enforce tenancy across MCP, Web UI, and CLI

```
CONTEXT: Thread the authenticated (TenantContext, Principal) through every human/agent surface so each is tenant-scoped end to end. Never trust a client-supplied tenant.

OBJECTIVE: Scope the MCP server, the Web UI REST+SSE gateway, and the CLI to the resolved tenant + principal.

BUILD:
- MCP server: resolve (TenantContext, Principal) from the credential on every connection/call; every mnesis_* tool operates on that tenant's store with that principal's visibility; reject any attempt to reference another tenant. SSE/HTTP transport carries no client-settable tenant.
- Web UI gateway (REST+SSE): resolve tenant+principal from the session/JWT server-side; scope all endpoints and SSE streams; never read a tenant id from the client. The UI shows only the principal's visible data; the page reader, graph, chat, and ingestion screens are all tenant-scoped.
- CLI: a tenant context for tenant-scoped ops (from an explicit, authenticated config/profile - not an unauthenticated flag); refuse tenant ops without a resolved context. (Tenant lifecycle/admin lives in T7's admin surface.)
- A single choke point per surface does the resolution, so no handler can run without a tenant+principal.

CONSTRAINTS:
- No surface accepts a client-supplied tenant id; all derive it from the verified credential/session.
- Every handler/tool runs only within the resolved tenant + visibility; fail closed otherwise.
- SSE and any streaming are per-tenant.

ACCEPTANCE:
- tests/test_surface_isolation.py: with tenant-A and tenant-B credentials, no MCP tool, REST endpoint, or SSE stream returns B's data to A and vice-versa; a forged/extra tenant id in a request is ignored in favour of the credential's tenant; an unauthenticated request is denied; a tenant-A Web session cannot fetch a tenant-B page/graph/chat result through any endpoint. `pytest -q` green.

ON DONE: commit ("feat(tenancy): tenant enforcement across MCP, Web UI, CLI"), report the per-surface choke points.
```

---

## Prompt T6 — Multitenant agent layer

```
CONTEXT: The agent families (writing/action/maintenance, plus egress) must run per-tenant - each agent confined to one tenant's data, tokens, schedules, queues, egress config, and audit.

OBJECTIVE: Make the agentic runtime tenant-aware: per-tenant agent instances reaching only their tenant's Mnesis, with per-tenant governance state.

BUILD:
- Each agent runs under a tenant-scoped credential (its MCP token resolves to that tenant+an agent principal), so via T3/T5 it can only reach its tenant's Mnesis. The runner hosts per-tenant agent instances and never shares a store/registry across tenants.
- Per-tenant governance state: the maintenance dream-cycle schedule, writing dead-letter/processed-state, action proposals/approvals, the egress allowlist + endpoint allowlist + quotas + kill-switch + send audit, and the run audit log are ALL per-tenant. Tenant A's agent cannot read B's data, use B's egress config, or write to B's audit.
- The dream cycle runs per-tenant (each tenant curates its own knowledge); writing connectors are per-tenant (a tenant's inbox is its own); action/egress operate within the tenant.
- Resolution is fail-closed: an agent without a resolvable tenant credential does not start.

CONSTRAINTS:
- An agent reaches Mnesis only via its tenant-scoped MCP credential; no agent can address another tenant's store, tokens, egress, or audit.
- All agent-side governance state is partitioned per tenant.
- The recipient/egress rules from the external-send set still hold, now per-tenant.

ACCEPTANCE:
- tests/test_agent_tenancy.py (stub): a tenant-A agent reaches only tenant-A Mnesis; its proposals/audit/egress-allowlist are isolated from tenant B's; a maintenance cycle for A curates only A; an attempt to use B's egress config or audit from A is refused; an agent with no resolvable tenant won't start. `pytest -q` green.

ON DONE: commit ("feat(tenancy): per-tenant agent layer"), report the per-tenant governance state list.
```

---

## Prompt T7 — Tenant lifecycle, admin, quotas, deploy, verify

```
CONTEXT: Operationalize multitenancy: provisioning, an admin boundary separate from tenant principals, per-tenant quotas, deployment, and the full isolation verification.

OBJECTIVE: Tenant lifecycle + a system-admin surface, per-tenant quotas, Compose/env wiring, docs, and the cross-tenant isolation drills.

BUILD:
- Lifecycle: provision a tenant (create root + init git + init caches + issue initial admin credential), list, suspend (deny access, retain data), and delete (remove the tenant's root, caches, credentials, and agent state - with a guarded confirm). All lifecycle ops are audited in a system (not tenant) audit log.
- Admin boundary: a SYSTEM-ADMIN principal (distinct from any tenant principal) is the only one who can manage tenants; tenant principals can never perform lifecycle ops or see other tenants. Admin actions are authenticated and audited.
- Quotas: per-tenant resource limits (storage, page count, request/agent rate) for fairness and blast-radius - fail closed when exceeded, surfaced clearly.
- Deploy: Compose/env for the tenant registry, credential store, and admin surface; document per-tenant encryption-at-rest as an optional hardening (per-tenant keys). Default deployment supports both single-tenant (`default`) and multi-tenant.
- Docs: README "Multitenancy" - the isolation-by-construction model, tenant-from-credential-only, within-tenant visibility, the admin boundary, quotas, and the migration path. CLAUDE.md: Mnesis is multitenant; stores are physically per-tenant; tenant derives from the credential; cross-tenant access is structurally impossible.

CONSTRAINTS:
- Only the system-admin can manage tenants; tenant principals are confined to their tenant.
- Tenant deletion removes ALL of a tenant's data/credentials/agent state; lifecycle ops are audited.
- Single-tenant deployments keep working via `default`.

ACCEPTANCE:
- The cross-tenant isolation DRILLS pass end to end: provision tenants A and B; via every surface (CLI, MCP, Web UI) and via agents, A cannot read, search, traverse, or receive B's data; a forged tenant id never crosses the boundary; a private resource never leaks within a tenant; suspend denies access while retaining data; delete removes everything and is audited; quotas fail closed; the `default`-tenant migration preserves prior behaviour. Full suite green; `docker compose up` works single- and multi-tenant.

ON DONE: commit ("feat(tenancy): lifecycle, admin, quotas, deploy, docs"), report the admin boundary and the isolation drill results.
```

---

## Verifying multitenancy (after T7) — the isolation drills

These negative tests are the bar. Each must hold across **every** surface and the agent layer.

1. `pytest -q` green; existing single-tenant behaviour intact under `default`.
2. **No global store:** the data layer cannot be used without a `TenantContext`; there is no ambient store.
3. **Physical separation:** tenants A and B occupy separate roots, git repos, and cache DBs; no shared file or table mixes them.
4. **Tenant from credential only:** a request/header/path/body carrying another tenant id is ignored in favour of the credential's tenant; unauthenticated/unresolved is denied (fail closed).
5. **No cross-tenant read anywhere:** via CLI, MCP tools, REST, SSE, search, graph, traverse, chat, or an agent, A never receives B's data — and vice-versa.
6. **Path traversal blocked:** crafted paths/ids can't escape a tenant root.
7. **Within-tenant visibility:** a private resource is invisible to other principals; shared is visible; roles gate writes.
8. **Agents partitioned:** a tenant's agents, schedules, proposals, egress config, and audit are isolated; A can't use B's egress or audit.
9. **Admin boundary:** only the system-admin manages tenants; tenant principals can't; lifecycle is audited.
10. **Lifecycle safe:** suspend denies but retains; delete removes everything and is audited; quotas fail closed.

If all ten hold, "user A can never access user B's data" is a structural guarantee — enforced by how the system is built, not by remembering to filter.

---

## Notes for running with Claude Code

- Run T1 → T7 in order on Opus 4.8. T1–T2 are the isolation primitive (do not move on until per-tenant stores/caches are proven); T3–T4 add auth + visibility; T5–T6 enforce across surfaces and agents; T7 operationalizes and runs the drills.
- The judgements that matter most, all of which must hold:
  - **Isolation by construction** — no global store; the store is only reachable through a `TenantContext`; paths can't escape the tenant root.
  - **Tenant derives solely from the verified credential** — never from request, header, path, or content; fail closed when unresolved.
  - **The negative test is the acceptance bar** — every surface must prove A cannot reach B.
- Keep it backward compatible: existing data migrates into `default`, and single-tenant deployments must keep working throughout.
- This set assumes a tenant can be a user or an org; if you later add SSO/an identity provider, resolve it to the same {tenant, principal, role} at the boundary — the rest of the system doesn't change.
- Encryption-at-rest per tenant and cross-tenant shared knowledge are deliberate non-goals here; if you add either, do it as an explicit, audited mechanism on top of this foundation, never as a relaxation of the isolation rules.
```
