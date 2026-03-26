# Agent Guidelines

## Git Branching Strategy

**Never commit directly to `develop` or `main`.**

1. **Feature/fix branches**: Always create a `feat/` or `fix/` branch off `develop` for your work.
2. **Merge to develop**: Once the feature branch is ready, merge it into `develop` (typically via PR).
3. **Merge to main**: Only merge `develop` into `main` when explicitly requested by the user.

```
feat/my-feature  →  develop  →  main
fix/my-bugfix    →  develop  →  main
```

## Docs And Execution Contract

- `docs/research/` is the target-shape, evidence, and comparative-truth layer.
- `docs/implementation/` is the shipped-truth and strategic implementation layer for `develop`.
- `docs/docs/` is the archive and historical layer.
- The GitHub Project is the execution layer.
- GitHub issues and PRs are the active work-tracking layer.
- PR bodies carry branch-specific scope, validation, and review receipts.
- Do not use docs as a live queue, branch tracker, or kanban mirror.

## Project Board Flow

- When a tracked issue is created or refined, set `Queue`, `Lane`, `Priority`, `Size`, `Status=Todo`, `Code Review=Not Ready`, and `PR=Not Ready`.
- When work starts, set `Status=In Progress` and move `Queue=Now` if the task is active now.
- When a PR opens, link it to the issue, set `PR=Open`, and set `Code Review=Pending`.
- While review is running, set `Code Review=Running`, then move to `Changes Requested` or `Passed`.
- When the PR merges, set `PR=Merged` and `Status=Done`.

## Review Rule

- Run a subagent review for non-trivial PR-sized slices.
- Verify subagent claims before acting on them.
- Record material review findings, or an explicit no-findings result, in the PR body and in affected implementation docs when the slice changes shipped truth or workflow contract.
