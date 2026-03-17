---
title: 08. Docs Contract
---

# 08. Docs Contract

## Status On `develop`

- [x] Seraph now has an explicit docs contract, but it still depends on contributors following it consistently.

## Paired Research

- evidence and comparison rules: [09. Reference Systems And Evidence](/research/reference-systems-and-evidence)

## Purpose

This file explains how research claims become implementation truth.

It is a cross-cutting implementation mirror, not a workstream doc.

Research and implementation should not compete with each other:

- research owns target product shape, evidence rules, benchmark logic, and superiority logic
- implementation owns shipped truth on `develop`, delivery ownership, and the live 10-PR queue

If a major concept appears in research but not implementation, or vice versa, the docs are incomplete.

## Ownership Rules

- [x] `docs/research/00-synthesis.md` owns the target-product synthesis
- [x] `docs/research/10-competitive-benchmark.md` owns the comparative judgment
- [x] `docs/research/11-superiority-program.md` owns the design-level superiority program
- [x] `docs/implementation/STATUS.md` owns the fastest shipped snapshot
- [x] `docs/implementation/00-master-roadmap.md` owns the live 10-PR queue and refresh rule
- [x] `docs/implementation/09-benchmark-status.md` mirrors the benchmark on shipped implementation terms
- [x] `docs/implementation/10-superiority-delivery.md` mirrors the superiority program on delivery terms

## Update Rules

- [x] If research adds a new benchmark or program layer, implementation needs a mirror doc in the same PR.
- [x] If implementation changes shipped truth, update `STATUS.md`, the owning workstream file, and any affected mirror docs in the same PR.
- [x] If the live queue changes, update the roadmap first, then any docs that summarize or justify that queue.
- [x] Open PRs and stacked branch state belong in PR bodies, not in docs that claim to describe `develop`.

## Parity Rules

- [x] Every implementation workstream should link to its paired research doc or docs.
- [x] Research should not carry a stale duplicate of the live PR queue.
- [x] Implementation should not make benchmark or superiority claims without a research-side source.
- [ ] The trees are only “in parity” when a reader can move from synthesis -> benchmark/program -> shipped implementation without guessing which file owns what.

## Acceptance Checklist

- [x] The docs now explain where research truth ends and implementation truth begins.
- [x] The benchmark/program layers have explicit implementation mirrors.
- [x] The entry-point docs can link a reader to the right owner instead of repeating stale fragments.
- [ ] Future PRs still need to follow this contract for the parity fix to hold.
