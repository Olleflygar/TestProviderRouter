# Nygen ProviderRouter Agent Guide

These repository-wide rules apply to every file in this repository.
Package-local `AGENTS.md` files add rules for their own directory trees; they
supplement rather than replace this file.

Use the current source and tests as the primary description of shipped
behavior. Use `Projectplan/NewProjectPlan.md` for the current roadmap and
`Projectplan/OldProjectPlan.md` for historical rationale. When they disagree,
apply them in that order. The two rules below preserve the testing philosophy
from the plans without requiring it to be copied into package-local guidance.

## Testing rules (non-negotiable)

1. No monkeypatching of internal collaborators. Do not use
   `monkeypatch.setattr(...)` or `unittest.mock.patch(...)` to reach into a
   module and swap out a class/function/attribute it references internally.
   Production code must expose a real seam instead -- a constructor
   parameter, an injectable factory/protocol, or another already-public
   extension point -- so tests depend on the public API, not on internal
   module paths. `monkeypatch.setenv`/`delenv` for environment variables is
   fine; that sets process state the code is meant to read, not a fake
   collaborator.

2. Do not delete existing tests as the project grows unless completely
   necessary, and only after careful consideration. New PRs add new test
   files alongside existing ones; existing tests keep acting as regression
   coverage. Only update a test (never delete outright) when a later PR
   deliberately changes the exact behavior that test was asserting.

## Retry boundary

PR27's same-provider retry is shipped and remains independent from the
provider-ordering `Policy` protocol. It is disabled unless callers explicitly
pass `retry_policy=`. Router-controlled retries are bounded, pre-open only, and
record every physical attempt in health, metrics, and exhaustion diagnostics.
Do not describe transient error categories as replay-safe: native arguments are
opaque, and retries can duplicate work, side effects, background/stored
operations, or charges.

## Storage schema administration

PR13's local administration foundation and PR30's storage-side score aggregation
are shipped. `MetricsStore` is a mandatory runtime-only three-method protocol:
`record_attempt`, raw-history `query_recent`, and bounded
`query_score_aggregates`. `ScoreBasedPolicy` always makes exactly one aggregate
call when history is enabled and never falls back to `query_recent`; backend SQL
returns intermediate totals, while `ProviderStats` and the final score remain
shared Python.

Schema inspection, creation, and migration stay on the separate typed
`nygen_router.storage.admin` surface and the `nygen-router storage ...` CLI.
Normal DuckDB/SQLite use may create metrics version 2 only when the configured
file path is absent. Existing version-1 and exact implicit PR29 targets are
recognized read-only but are not runtime-compatible and have no PR30 migration
route. An existing file is never silently initialized, stamped, migrated,
reindexed, replaced, renamed, deleted, redirected, copied, or switched. Keep
future component revisions independent in `nygen_router_schema_versions`, and
keep any future migration steps explicit, transactional, offline, consecutive,
and version-last.
