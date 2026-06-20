# Agent Guidelines

## Git Branching Strategy

**Never commit directly to `develop` or `main`.**
**Never create draft pull requests.**
**Pull requests should complete whole milestones or batch-sized work, not tiny slices.**
**When the user directs a stacked batch train, do all selected board batches as stacked ready PRs and report back only when the stack is complete.**

1. **Feature/fix branches**: Always create a `feat/` or `fix/` branch for your work. By default branch from `develop`.
2. **Stacked batch trains**: If the user explicitly asks to stack board batches, create the first batch branch from `develop`, then create each following batch branch from the previous batch branch. Open each PR against the previous branch so the stack can be reviewed and merged in order.
3. **Merge to develop**: For normal unstacked work, merge the feature branch into `develop` via PR. For stacked trains, merge the stack in order until the first branch lands in `develop`.
4. **Merge to main**: Only merge `develop` into `main` when explicitly requested by the user.
5. **Ready PRs only**: Pull requests must be opened ready for review unless the user explicitly requests a draft.
6. **Batch scope**: Default PR scope is a complete milestone or substantial batch. Use issue checklists, child issues, and internal commits for slices, but keep the team working until the batch acceptance criteria are complete.

```
feat/my-feature  →  develop  →  main
fix/my-bugfix    →  develop  →  main

feat/batch-one  →  develop
feat/batch-two  →  feat/batch-one
feat/batch-three → feat/batch-two
```

## Docs And Execution Contract

- `docs/research/` is the target-shape, evidence, and comparative-truth layer.
- `docs/implementation/` is the shipped-truth and strategic implementation layer for `develop`.
- `docs/docs/` is the archive and historical layer.
- The GitHub Project is the execution layer.
- GitHub issues and PRs are the active work-tracking layer.
- PR bodies carry branch-specific scope, validation, and review receipts.
- Do not use docs as a live queue, branch tracker, or kanban mirror.

## Team Lead Operating Model

For substantial planning, roadmap, architecture, implementation, or review work, Codex acts as the team lead.

Substantial work includes any task that changes strategy or docs truth, touches two or more modules, affects security, memory, runtime, agent behavior, project tracking, or requires validation beyond a single narrow check.

- The lead owns the plan, decomposition, sequencing, scope boundaries, tradeoff calls, and final synthesis.
- The lead must create or update the agent team to fit the task, plan, and risk profile before execution starts.
- The agent team should fit the work. Typical roles include Planner, Explorer, Worker, Security, Memory, Docs, Integrator, and Critic/Contrarian. If subagent tooling is unavailable, the lead must run separate named passes and state that limitation.
- The lead must delegate bounded work to agents with explicit ownership, file or module scope, acceptance criteria, proof requirements, and expected output.
- The lead must plan batches around whole milestones or substantial milestone slices, then keep the team working until the batch is complete rather than opening partial PRs for individual micro-slices.
- The lead must not directly implement substantial feature slices when a suitable worker agent can own them; the lead coordinates, reviews, integrates, and decides. The lead may directly implement small surgical changes, emergency fixes, or work where delegation tooling is unavailable, but must state the reason.
- If agent capacity or tooling prevents delegation, the lead must state that limitation and keep any direct edits tightly scoped.
- Before execution starts, the lead must confirm the branch is not `develop` or `main` and follows the `feat/` or `fix/` branch rule.
- Delegated agents are not alone in the codebase. They must not revert unrelated edits, rewrite strategy, broaden scope, or change milestone order without lead direction.
- The lead owns GitHub Project correctness when creating or refining tracked work, including the fields defined in Project Board Flow.
- The lead must verify material subagent claims before using them for code changes, commits, PRs, project updates, issue updates, release notes, roadmap decisions, or strategic claims.
- PR bodies for substantial work should summarize the agent team used, the Critic/Contrarian result, and the verification performed.

### Required Critic / Contrarian Role

Every non-trivial plan, roadmap, competitive analysis, architecture change, security-sensitive change, memory change, or PR-sized slice must include at least one Critic/Contrarian agent pass.

The Critic/Contrarian must be independent from the worker assumptions. No same-pass self-approval: the critic receives the plan, diff, evidence, or issue set and produces a separate critique.

The Critic/Contrarian agent checks:

- hallucinations, weak evidence, and unsupported competitive claims
- stale assumptions about current products, agents, models, APIs, or security posture
- missing current-source verification for any competitive or modern technical claim likely to have changed; temporally unstable claims about competitors, models, APIs, security posture, releases, or current product capabilities require official/current source URLs and dates
- missing acceptance criteria, proof, evals, or operator-visible receipts
- security, privacy, memory, and trust-boundary gaps
- scope creep, vague milestones, duplicate issues, and time-bounded roadmap drift; before creating issues, search open and closed issues for similar scope
- contradictions between docs, GitHub Project state, issues, PRs, and shipped behavior where relevant
- weak evidence standards: competitive claims need source URLs, code claims need file paths or line numbers, and GitHub/Project claims need issue, PR, or project item IDs
- false completion claims; do not imply files, issues, tests, project fields, or PR state changed unless tools confirm it

The lead must run the Critic/Contrarian pass before irreversible project actions such as issue creation, PR creation, branch merge, roadmap finalization, or public superiority claims.

The lead should incorporate the critique, explicitly reject it with rationale, or turn it into follow-up issues before finalizing. Record the disposition as accepted, rejected, or deferred in the PR body, issue comment, final response, or relevant docs.

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

- Every PR-sized slice must be reviewed before merge. Non-trivial PR-sized slices require an independent subagent review.
- Every pushed update to an open PR that changes behavior, security/privacy posture, runtime wiring, settings, docs truth, tests, or workflow contract must receive a fresh independent Critic/Contrarian pass before the lead claims the PR is reviewed, updates the PR as review-passed, or asks to merge.
- Small follow-up commits are not exempt when they affect the same PR's acceptance criteria or operator-visible behavior. Treat them as part of the PR-sized slice and re-run the critic on the cumulative diff or the changed follow-up scope.
- A lead's own named "critic pass" is not a substitute when subagent tooling is available. Use an independent subagent critic; only fall back to a separate self-run critic pass when subagent tooling is unavailable, and state that limitation in the PR/final response.
- Verify subagent claims before acting on them.
- Record material review findings, or an explicit no-findings result, in the PR body and in affected implementation docs when the slice changes shipped truth or workflow contract.
- Do not merge a PR until material review findings are either fixed, explicitly rejected with rationale, or turned into tracked follow-up work.
