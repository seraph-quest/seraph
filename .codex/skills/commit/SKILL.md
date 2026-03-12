---
name: commit
description:
  Create a well-formed git commit from current changes using session history for
  rationale and summary; use when asked to commit, prepare a commit message, or
  finalize staged work.
---

# Commit

## Goals

- Produce a commit that matches the actual code changes.
- Follow conventional commit style with a concise subject and informative body.
- Respect this repo's branch policy: never commit directly on `main` or
  `develop`.

## Inputs

- Codex session history for task intent and rationale.
- `git status`, `git diff`, and `git diff --staged` for the actual changes.
- Repo guidance from `AGENTS.md`.

## Steps

1. Confirm the current branch is not `main` or `develop`.
2. Inspect the working tree and staged changes.
3. Stage only the intended files.
4. Sanity-check new files so that logs, temp files, generated artifacts, and
   local env files are not accidentally committed.
5. Choose a conventional commit type that matches the change.
6. Write a subject line in imperative mood, 72 characters or fewer.
7. Write a wrapped body that includes:
   - what changed,
   - why it changed,
   - validation run, or an explicit note if none was run.
8. Add `Co-authored-by: Codex <codex@openai.com>` unless the user asked for a
   different identity.
9. Create the commit with `git commit -F <file>` or an equivalent literal
   multi-line message flow.

## Template

```text
<type>(<scope>): <short summary>

Summary:
- <what changed>
- <what changed>

Rationale:
- <why>
- <why>

Tests:
- <command or "not run (reason)">

Co-authored-by: Codex <codex@openai.com>
```
