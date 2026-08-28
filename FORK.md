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
| CI triggers include `zeno`; fork docs | (bootstrap) | not for upstream |
| Pluggable task execution backend: `MCPTaskStore` keeps records + `record_status`; `TaskExecutionBackend` (`start`/`cancel`/`deliver_input`) owns execution; `AsyncioTaskBackend` default preserves 0.13.0 behavior | (s1) | PR candidate |
| Progress notifications: `params._meta.progressToken` threads into tool execution; `ProgressReporter` emits `notifications/progress` (kind detail in `_meta`) from tools and task backends | (s2) | PR candidate |
| MCP Apps extension: `MCPConfig.apps` handshake (`io.modelcontextprotocol/apps`); `ui://` resources visible only to capable clients of an apps-enabled server, inert otherwise | (s3) | PR candidate (waits on upstream apps posture) |

## Constraints

- The fork stays free of zeno-specific types and never imports a workflow
  engine; every functional delta must remain a candidate upstream pull
  request.
- The dist and import name do not change (`litestar-mcp` /
  `litestar_mcp`).
- Upstream's `publish.yml` / `cd.yml` / `docs.yml` workflows are inert
  here (no releases are cut from this repository); the fork is consumed
  by git rev only.
