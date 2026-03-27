---
title: 08. Docs Contract
---

# 08. Docs Contract

## Status On `develop`

- [x] Seraph now has an explicit docs contract, but it still depends on contributors following it consistently.

## Paired Research

- evidence and comparison rules: [09. Reference Systems And Evidence](/research/reference-systems-and-evidence)

## Purpose

This file explains how research truth, implementation truth, and GitHub execution state fit together.

It is a cross-cutting implementation mirror, not a workstream doc.

Research and implementation should not compete with each other:

- research owns target product shape, evidence rules, benchmark logic, and superiority logic
- implementation owns shipped truth on `develop`, delivery ownership, and strategic implementation translation
- GitHub owns active execution state, review state, and merge state

If a major concept appears in research but not implementation, or vice versa, the docs are incomplete.

## Ownership Rules

- [x] `docs/research/00-synthesis.md` owns the target-product synthesis
- [x] `docs/research/10-competitive-benchmark.md` owns the comparative judgment
- [x] `docs/research/11-superiority-program.md` owns the design-level superiority program
- [x] `docs/implementation/STATUS.md` owns the fastest shipped snapshot
- [x] `docs/implementation/00-master-roadmap.md` owns the strategic implementation program, completed-program record, and workstream-to-gap translation
- [x] `docs/implementation/09-benchmark-status.md` mirrors the benchmark on shipped implementation terms
- [x] `docs/implementation/10-superiority-delivery.md` mirrors the superiority program on delivery terms
- [x] the GitHub Project owns execution state
- [x] GitHub issues and PRs own active work tracking, review state, and merge state
- [x] `docs/docs/` owns historical/archive material that is no longer current truth

## Update Rules

- [x] If research adds a new benchmark or program layer, implementation needs a mirror doc in the same PR.
- [x] If implementation changes shipped truth, update `STATUS.md`, the owning workstream file, and any affected mirror docs in the same PR.
- [x] If active execution state changes, update the GitHub Project, issue, or PR instead of mirroring that state in docs.
- [x] If the strategic implementation program changes materially, update the roadmap and any affected mirror docs in the same PR.
- [x] Open PRs, review state, and branch state belong in issues, PRs, and the GitHub Project, not in docs that claim to describe `develop`.

## Project Workflow

Project fields:

- `Queue`: `Now`, `Next`, `Background`, `Blocked`
- `Status`: `Todo`, `In Progress`, `Done`
- `Lane`: `Trust Boundaries`, `Execution Plane`, `Runtime Reliability`, `Presence and Reach`, `Guardian Intelligence`, `Embodied UX`, `Ecosystem and Leverage`, `Docs / Meta`
- `Priority`: `P0`, `P1`, `P2`
- `Size`: `S`, `M`, `L`
- `Code Review`: `Not Ready`, `Pending`, `Running`, `Passed`, `Changes Requested`
- `Linked pull requests`: built-in GitHub linkage
- `PR`: `Not Ready`, `Open`, `Merged`

Expected flow:

1. Create or refine the tracked issue.
   - set `Queue`, `Lane`, `Priority`, `Size`
   - set `Status=Todo`
   - set `Code Review=Not Ready`
   - set `PR=Not Ready`
2. Start execution.
   - set `Status=In Progress`
   - move `Queue=Now` if the task is active now
3. Open the PR.
   - link the PR to the issue
   - keep the issue as the project item and use built-in linked pull requests for the PR relationship
   - set issue `PR=Open`
   - set issue `Code Review=Pending`
4. Run review.
   - set `Code Review=Running` while review is active
   - move to `Changes Requested` or `Passed`
5. Merge and close.
   - set `PR=Merged`
   - set `Status=Done`

## Batch Mode

Use this as the default execution model when several internal slices will land through one GitHub PR.

- create one parent batch issue as the project item
- keep one aggregate PR linked to that parent issue
- use the parent batch issue checklist as the authoritative internal-slice list by default
- only create child slice issues when a slice has separate ownership, is a blocker, has independent acceptance criteria, or could be reprioritized separately
- if child slice issues exist, they may carry their own `Queue` and `Status`, but leave `PR=Not Ready` and `Code Review=Not Ready` unless that slice gets its own PR
- do not mirror one aggregate PR across all child slice issues; the parent batch issue remains the main project item for aggregate `PR` and `Code Review` state
- use issue comments or the PR body for receipts and progress notes, not as a second authoritative slice-state surface

## Latest Workflow Contract Update

- [x] GitHub Project now owns the active execution layer for Seraph.
- subagent review:
  - reviewer: `Fermat` (`019d2bff-cf88-70f0-a8cd-772182c834d1`)
  - concrete findings fixed before the slice stayed final:
    - removed tracked issue form fields that duplicated `Queue`, `Lane`, `Priority`, and `Size` outside the project
    - removed manual `Queue`, `Lane`, `Code Review`, and `PR` fields from the PR template so PR bodies stop mirroring board state
    - removed current workflow guidance from `docs/docs/setup.md` so the archive tree stays archival
    - normalized project seeding to issue-first tracking with built-in linked pull requests instead of a mixed issue-plus-standalone-PR model
    - corrected the repo-local push skill so it no longer falsely implies automatic board mutation
  - final verification:
    - project item `#233` links PR `#238` with `PR=Open` and `Code Review=Pending`
    - project item `#239` links PR `#232` with `PR=Open` and `Code Review=Pending`
    - the project no longer uses standalone PR items for tracked work that already has an issue item
- [x] Batch execution now defaults to one parent issue plus one aggregate PR.
- subagent follow-up review:
  - reviewer: `Fermat` (`019d2bff-cf88-70f0-a8cd-772182c834d1`)
  - concrete findings fixed before the slice stayed final:
    - removed PR-template wording that would duplicate internal slice tracking in the PR body
    - made the parent batch issue checklist the authoritative batch-decomposition surface unless a slice becomes its own child issue
    - defined child-slice board semantics under an aggregate PR: child issues may move through `Queue` and `Status`, but keep `PR=Not Ready` and `Code Review=Not Ready` unless they get their own PR
    - clarified the tracked-work issue template so the batch-decomposition section is only for parent batch issues and can be left blank on standalone or child issues
  - final verification:
    - the parent batch issue owns aggregate `PR` and `Code Review` state
    - child slice issues no longer imply duplicated aggregate-PR state
    - issue comments and PR bodies are now receipts and notes, not a second authoritative slice-status surface

## Parity Rules

- [x] Every implementation workstream should link to its paired research doc or docs.
- [x] Research should not carry a stale duplicate of GitHub execution state.
- [x] Implementation should not make benchmark or superiority claims without a research-side source.
- [ ] The trees are only “in parity” when a reader can move from synthesis -> benchmark/program -> shipped implementation without guessing which file owns what.

## Acceptance Checklist

- [x] The docs now explain where research truth ends and implementation truth begins.
- [x] The benchmark/program layers have explicit implementation mirrors.
- [x] The entry-point docs can link a reader to the right owner instead of repeating stale fragments or stale board state.
- [ ] Future PRs still need to follow this contract for the parity fix to hold.
