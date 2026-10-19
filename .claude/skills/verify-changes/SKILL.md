---
name: verify-changes
description: Verify the current change before calling it done. Lists changed files (committed on the branch and uncommitted), runs the fast static checks, then only the dependency-selected tests, and reports real exit codes. Use after implementing or before asking for review.
---

Rules come from `AGENTS.md` (Validation Matrix, CI And Release Gates). Link there; do not restate them.

1. **Changed files.** Run `git status --short` and `git diff --name-only $(git merge-base HEAD develop)` (use the epic branch from the owning issue if the PR does not target `develop`). Include untracked files. Group them: backend, frontend, docs, scripts, `.claude`/`AGENTS.md`.
2. **Fast static checks** for the groups that changed:
   - docs or `AGENTS.md`: `python3 scripts/check_docs_contract.py` and `python3 scripts/check_strategy_claims.py`; for docs site changes also `npm run typecheck` in `docs/`
   - frontend: `npx tsc -b` in `frontend/`
   - `AGENTS.md`, `package.json`, `.claude/**`: `npm run format:check` at the repo root
   - backend has no linter or type checker; do not claim one ran
3. **Tests, dependency-selected only.** No full backend suite and at most `xargs -P 4`.
   - Backend: the changed test files, tests that import the changed modules (`grep -rl` the module name under `backend/tests`), `backend/tests/test_model_fabric_dispatch_inventory.py`, and the docs contract. Run with `cd backend && uv run pytest --no-cov -q <files>`.
   - Frontend: `npm test -- <files>` in `frontend/`, only if frontend files changed.
   - Docs changes: `npm run build` in `docs/` before pushing (MDX rejects bare `<placeholder>` text).
4. **Disabled scopes.** The repo defines none. If the issue, PR or operator disables a test scope, skip it and say so with the residual risk.
5. **Report** each command with its actual exit code and the failing test names. Name every skipped check with a reason. Never write "tests pass" for something that was not run.

Per change, steps 1-5 are enough. The full verify (hosted CI shards, current CI failures fixed, affected local and hosted checks rerun) is required only at the `develop` -> `main` boundary or a release, per `AGENTS.md` "CI And Release Gates".
