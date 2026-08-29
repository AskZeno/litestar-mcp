# Platform fork — AskZeno/litestar-mcp

This is the AskZeno platform fork of
[cofin/litestar-mcp](https://github.com/cofin/litestar-mcp), decided by
monorepo ADR-0087 (fork litestar-mcp for durable task backends, apps, and
progress) and executed by
`docs/plans/2026-08-28_fork-litestar-mcp.plan.md`.

## Branch discipline

- `main` mirrors upstream `cofin/litestar-mcp` and carries no fork
  commits. Syncing upstream = fast-forwarding `main`.
- `zeno` (default) is the delta branch and the monorepo pin target. It is
  based on the `v0.13.0` tag — the exact release the monorepo lockfile
  resolved from PyPI — and carries the fork deltas as small, individually
  upstreamable commits.
- Rebasing `zeno` onto a newer upstream release is a deliberate act that
  rides a monorepo pin bump, never an ambient merge.

## Version scheme

Fork versions are local versions on the pinned release
(`0.13.0+zeno.N`), so every existing `>=0.13.0,<0.14` constraint keeps
resolving. `N` increments per delta wave; the git rev recorded in the
monorepo `uv.lock` is the actual pin. `0.13.0` unversioned means zero
delta (bootstrap state).

## Divergence ledger

Every fork delta is listed here with its upstream posture. Any accepted
upstream change rebases this branch smaller.

| Delta | Commits | Upstream posture |
| --- | --- | --- |
| CI triggers include `zeno`; fork docs | `8b45dda`, `d78f2a4`, `55d494d` | not for upstream |
| Pluggable task execution backend: `MCPTaskStore` keeps records + `record_status`; `TaskExecutionBackend` (`start`/`cancel`/`deliver_input`) owns execution; `AsyncioTaskBackend` default preserves 0.13.0 behavior | `284e2fc` | PR candidate (`upstream/task-backend-seam`) |
| Protocol-standard unlimited task lifetime: `MCPTaskConfig.default_ttl_ms=None` persists `ttlMs: null` records without store expiry for durable application operations | current `zeno.3` wave | task-seam PR candidate |
| Request-scoped progress: `params._meta.progressToken` threads through `ProgressReporter`; HTTP SSE and stdio deliver notifications on the owning request before its final response, and disconnect cancels dispatch | `c19c21a`, `a80c762` | PR candidate (`upstream/progress-notifications`) |
| Task subscription conformance: `notifications.taskIds` requires the `io.modelcontextprotocol/tasks` client capability and returns the shared `-32021` payload otherwise | `ad8d1bb` | PR candidate (task seam follow-up) |
| MCP Apps server contract: official `io.modelcontextprotocol/ui` identifier + `mimeTypes`, `_meta.ui` tool/resource linkage, profile MIME type, capability degradation, and startup validation | `8ec7dd9`, `bf5b07c` | PR candidate (waits on upstream apps posture) |
| Honest completions: registry-owned prompt/resource completers; capability and method exist only when a provider is registered | `f6db631` | PR candidate |
| Pluggable tool type adapters shared by validation and JSON Schema; msgspec terminal default, guarded Pydantic integration with host auto-detection, UUID format support, and root-valid model definitions | `f7fc013`, `bc3cf4f` | PR candidate |
| Generic product-extension seams: opt-key flat-body schema projection, one request-scoped discovery/invocation policy, and complete request metadata on `MCPRequestContext` | `46303fb` | PR candidate |
| Handled exception responses run through the synthetic ASGI send lifecycle so request cleanup hooks fire | `09f9bba` | PR candidate |
| MCP 2026-07-28 conformance sweep pins sentinel headers, notification POST posture, 405s, extension errors, and resource-not-found data | `e3aac34` | test-only upstream candidate |

## Conformance

Conformance against MCP 2026-07-28, the tasks extension (SEP-2663), and
the apps extension (SEP-1865) is tracked in the monorepo's
`docs/plans/mcp-conformance-gap-ledger.md` (G1-G8). The `0.13.0+zeno.3`
wave implements every fork-surface row: G1 request-owned progress, G2
taskIds gating, G4 honest completions, and G6 the official Apps server
contract. G3 was retired as an audit error and G8 was already closed;
G7 remains a parked consumer-side task-handle migration.

## Upstream offering (ADR-0087 s8)

Two PR-ready branches are rebased onto ``upstream/main`` and kept green
against the full upstream suite:

- ``upstream/task-backend-seam`` — the execution-backend split
  (record persistence + ``record_status`` vs ``TaskExecutionBackend``,
  ``AsyncioTaskBackend`` default). Behavioral no-op for existing users;
  the pre-existing tasks suite passes unmodified.
- ``upstream/progress-notifications`` — ``progressToken`` threading plus
  request-scoped HTTP SSE/stdio delivery and disconnect cancellation
  (stacked on the seam branch).

Pull requests against ``cofin/litestar-mcp`` open once the deltas have
baked in production (plan s8); any accepted change rebases ``zeno``
smaller. The apps extension waits on upstream's own apps posture.

## Constraints

- The fork stays free of zeno-specific types and never imports a workflow
  engine; every functional delta must remain a candidate upstream pull
  request.
- The dist and import name do not change (`litestar-mcp` /
  `litestar_mcp`).
- Upstream's `publish.yml` / `cd.yml` / `docs.yml` workflows are inert
  here (no releases are cut from this repository); the fork is consumed
  by git rev only.
