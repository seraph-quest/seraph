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
- Default batch mode: use one parent batch issue as the project item and one aggregate PR linked to that parent issue.
- Track internal slices in the parent batch issue itself by default. That parent issue checklist is the authoritative slice list unless a slice becomes its own child issue.
- Create child slice issues only when a slice has separate ownership, is a blocker, has independent acceptance criteria, or could be reprioritized separately.
- If child slice issues exist, they may carry their own `Queue` and `Status`, but keep `PR=Not Ready` and `Code Review=Not Ready` unless they get their own PR. Do not mirror one aggregate PR across every child issue.
- When work starts, set `Status=In Progress` and move `Queue=Now` if the task is active now.
- The issue remains the project item. Use built-in linked pull requests for the PR relationship instead of creating a second standalone project item for the same tracked work.
- When an aggregate PR opens, link it to the parent batch issue, set that issue `PR=Open`, and set that issue `Code Review=Pending`.
- While review is running, set `Code Review=Running`, then move to `Changes Requested` or `Passed`.
- When the PR merges, set `PR=Merged` and `Status=Done`.

## Review Rule

- Run a subagent review for non-trivial PR-sized slices.
- Verify subagent claims before acting on them.
- Record material review findings, or an explicit no-findings result, in the PR body and in affected implementation docs when the slice changes shipped truth or workflow contract.
