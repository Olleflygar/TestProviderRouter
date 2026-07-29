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
