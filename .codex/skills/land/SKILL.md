---
name: land
description:
  Land a PR by monitoring feedback, resolving conflicts against `develop`,
  waiting for checks, and squash-merging when green.
---

# Land

## Goals

- Ensure the PR is conflict-free with `develop`.
- Keep checks green and address review feedback before merge.
- Squash-merge the PR only when the branch is ready.

## Preconditions

- `gh` CLI is authenticated.
- You are on the PR branch with a clean working tree.
- The PR targets `develop`.

## Steps

1. Locate the PR for the current branch.
2. Confirm the required local validation already passes for the affected scope.
3. If the working tree has uncommitted changes, use the `commit` skill and then
   the `push` skill before continuing.
4. Check mergeability against `develop`.
5. If conflicts exist, use the `pull` skill to merge `origin/develop`, resolve
   conflicts, rerun validation, then use the `push` skill.
6. Sweep review feedback before merge:
   - top-level PR comments,
   - inline review comments,
   - review summaries and requested changes,
   - Codex review comments if present.
7. Use the async watcher when possible:
   - `python3 .codex/skills/land/land_watch.py`
8. If checks fail:
   - inspect the failing jobs,
   - fix the issue locally,
   - rerun validation,
   - commit and push,
   - restart the watcher.
9. When all feedback is addressed and checks are green, squash-merge the PR.

## Commands

```sh
branch=$(git branch --show-current)
pr_number=$(gh pr view --json number -q .number)
pr_title=$(gh pr view --json title -q .title)
pr_body=$(gh pr view --json body -q .body)
mergeable=$(gh pr view --json mergeable -q .mergeable)

if [ "$mergeable" = "CONFLICTING" ]; then
  echo "Run the pull skill, resolve conflicts, validate, and push." >&2
  exit 1
fi

python3 .codex/skills/land/land_watch.py
gh pr merge --squash --subject "$pr_title" --body "$pr_body"
```

## Notes

- Do not enable auto-merge for this workflow.
- Do not merge while review comments or requested changes are still open.
- If `origin/develop` is missing, stop and report it as a blocker.
