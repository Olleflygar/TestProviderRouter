# Nygen ProviderRouter Agent Guide

Repo-wide rules that apply to every PR package folder (ProviderRouterPR1,
ProviderRouterPR2, ...). Package-local AGENT.md files add rules specific to
that package; they do not replace these.

See `ProviderRouterPR1/Projectplan/ProjectPlan.md` for the full project plan
and the "Testing philosophy" section it contains -- the two rules below are
the summary of that section, repeated here so they apply automatically to
every future PR folder without needing to be copied into each one.

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
