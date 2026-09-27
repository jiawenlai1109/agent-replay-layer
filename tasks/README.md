# Task cards and fixtures

`<id>/task.md` is the only text the agent receives. `<id>/seed/` is the workspace handed
to it. The machine-checkable promises live in `../ledger/<id>/commitments.json` and are
never shown to the agent, otherwise the measured latency would be the agent echoing our
checklist back.

Two families differ in what ships here:

- `T-PIPE-*` and `N*` seeds are this project's own artifacts (a small CSV pipeline, a
  README to rewrite, a data insight note). They are committed.
- `A1` to `A4` seeds are upstream Python package trees at the commit just before the fix,
  so they are not redistributed. Rebuild them with `python3 make_family_a.py`, which
  fetches the base tree and the pull-request tree over `codeload` and records the commit
  and source URL into each task card. The suites that need them skip when they are absent.

`ledger/<id>/oracle/` holds copies of upstream test files used as the hidden judge. They
are gitignored and not redistributed. Without them the family-A predicates cannot run.
