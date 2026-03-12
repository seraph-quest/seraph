---
name: pull
description:
  Pull the latest integration branch into the current feature branch and
  resolve merge conflicts. Use when Codex needs to sync a feature branch with
  origin using a merge-based update.
---

# Pull

## Goals

- Sync the current feature branch with the repository integration branch.
- Preserve the repo's branch policy: feature branches merge into `develop`.
- Resolve conflicts carefully and rerun affected checks afterward.

## Default base branch

- Primary integration branch: `origin/develop`
- If `origin/develop` does not exist, stop and treat that as a workflow blocker.

## Workflow

1. Confirm the current branch is a `feat/*` or `fix/*` branch.
2. Verify the working tree is clean before merging. Commit or stash local work
   first if needed.
3. Enable rerere locally:
   - `git config rerere.enabled true`
   - `git config rerere.autoupdate true`
4. Fetch latest refs:
   - `git fetch origin`
5. Sync the remote feature branch first:
   - `git pull --ff-only origin $(git branch --show-current)`
6. Merge the integration branch:
   - `git -c merge.conflictstyle=zdiff3 merge origin/develop`
7. If conflicts appear:
   - inspect intent on both sides before editing,
   - resolve one file at a time,
   - remove all conflict markers,
   - stage the resolved files,
   - complete the merge commit.
8. Run affected validation for the changed area after the merge.
9. Record the merge result for the workpad:
   - merge source: `origin/develop`,
   - clean merge or conflicts resolved,
   - resulting short SHA.

## Conflict guidance

- Prefer the minimal resolution that preserves the feature branch intent.
- Avoid silently dropping either side's behavior.
- For import conflicts, temporarily keep both sides if needed and let tests or
  builds tell you what is still required.
- Check for leftover markers with `git diff --check`.

## When to ask the user

Proceed without asking unless the conflict requires choosing between
user-visible behaviors that cannot be inferred from code, tests, or docs.
