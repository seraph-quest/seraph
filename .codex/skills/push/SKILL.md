---
name: push
description:
  Push the current feature branch to origin and create or update the
  corresponding pull request targeting `develop`.
---

# Push

## Prerequisites

- `gh` CLI is installed and authenticated for this repository.
- The current branch is a feature or fix branch, not `main` or `develop`.
- `origin/develop` exists. If it does not, stop and surface that blocker.

## Goals

- Push the current branch safely.
- Create or update a PR targeting `develop`.
- Keep PR title, body, and validation evidence aligned with the actual diff.

## Validation gate

Run the narrowest required validation before each push:

- Backend changes:
  `cd backend && OPENROUTER_API_KEY=test-key WORKSPACE_DIR=/tmp/seraph-test uv run pytest -v`
- Frontend changes:
  `cd frontend && npm test`
- Docs changes:
  `cd docs && npm run build`
- Editor changes:
  `cd editor && npm run build`
- Multi-area changes: run every affected command.

Use `./.codex/worktree_init.sh` first if dependencies have not been installed in
the workspace yet.

## Steps

1. Identify the current branch and confirm the remote state.
2. Run required validation for the files changed in this branch.
3. Push to `origin`, using upstream tracking if needed.
4. If push is rejected because the branch is behind:
   - run the `pull` skill,
   - rerun validation,
   - push again.
5. If push fails due to auth, permissions, or branch protection, stop and
   report the exact error instead of changing remotes or protocols.
6. Ensure a PR exists for the branch targeting `develop`:
   - create one if missing,
   - update it if it already exists,
   - if the branch is tied to a closed or merged PR, create a fresh branch and
     PR instead of reusing it.
7. Write a concise PR body that reflects the full current scope using the
   repo-preferred checklist sections:
   - `## Done on develop`
   - `## Working in this PR`
   - `## Still to do after this PR`
    - `## Validation`
8. If the branch closes or advances a tracked issue, ensure the PR links that
   issue. The issue stays the project item; update that issue item's `PR` and
   `Code Review` fields after the PR exists.
9. Reply with the PR URL.

## Commands

```sh
branch=$(git branch --show-current)
test -n "$branch"

git fetch origin
git push -u origin HEAD

pr_state=$(gh pr view --json state -q .state 2>/dev/null || true)
if [ "$pr_state" = "MERGED" ] || [ "$pr_state" = "CLOSED" ]; then
  echo "Current branch is tied to a closed PR; create a fresh branch." >&2
  exit 1
fi

pr_title="<clear PR title>"
tmp_pr_body=$(mktemp)
cat >"$tmp_pr_body" <<'EOF'
## Done on develop
- [x] <already shipped baseline that this PR builds on>

## Working in this PR
- [ ] <concrete change in this PR>

## Still to do after this PR
- [ ] <real remaining work after merge>

## Validation
- <commands run>
EOF

if [ -z "$pr_state" ]; then
  gh pr create --base develop --title "$pr_title" --body-file "$tmp_pr_body"
else
  gh pr edit --base develop --title "$pr_title" --body-file "$tmp_pr_body"
fi

gh pr view --json url -q .url
rm -f "$tmp_pr_body"
```

## Notes

- Do not use `--force`; use `--force-with-lease` only if history was rewritten.
- Treat non-fast-forward failures as sync problems for the `pull` skill.
- Treat auth or workflow restriction failures as blockers, not as a prompt to
  rewrite remotes.
- Keep the checklist factual against `develop`. Do not mark branch-only work as
  done on `develop`.
- This repo does not use Symphony or Linear workflow automation.
- This skill does not update GitHub Project fields automatically. After the PR
  exists, update the linked issue's `PR` and `Code Review` fields through the
  project board or `gh project item-edit`.
