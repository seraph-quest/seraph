---
title: 08. Documentation Contract
---

# 08. Documentation Contract

The [Project Constitution](./00-project-constitution.md) is the sole authority
for Seraph's product definition and accepted target architecture. Its ADRs own
locked decisions. No roadmap, research document, issue, or archived strategy
document can define a competing target.

## Ownership

| Surface | Owns | Does not own |
| --- | --- | --- |
| Project Constitution and ADRs | Product definition, accepted target, vocabulary, invariants | Shipped capability claims or live execution state |
| `docs/implementation/STATUS.md` and implementation guides | Shipped/Partial behavior on `develop`, validation, operator contracts | Target architecture or project queue |
| `docs/research/` | Evidence, alternatives, dated comparisons, uncertainty | Accepted target or shipped truth |
| `/legacy` and explicit archived stubs | Historical context and links to Git history | Current instructions, roadmap, or supported behavior |
| GitHub Project, issues, and PRs | Queue, ownership, review, integration, completion receipts | Product constitution |

## Status Rules

Capabilities use exactly these lifecycle states:

- **Shipped** — available on `develop` with named proof.
- **Partial** — usable on `develop` with named missing boundaries.
- **Experimental** — opt-in canary with explicit limits and rollback.
- **Planned** — accepted tracked work with no shipped implementation.
- **Deprecated** — present temporarily with a replacement/removal issue.
- **Excluded** — intentionally outside Seraph's capability boundary.

**Target**, **Research**, **Archived**, and **Blocked** classify decisions,
documents, or work—not capabilities. A capability must never be labeled Target.
Configured state, a branch-local change, or a deterministic fixture is not
evidence of Shipped.

## Change Rules

- Change the accepted target only through the constitution and a new or
  superseding ADR, tracked issue, and independent Critic/Contrarian review.
- Update STATUS and the owning implementation guide when merged behavior changes.
- Preserve legitimate transitional shipped facts, but label Deprecated paths
  and name their replacement/removal issue.
- Keep research comparative claims dated, sourced, and separate from decisions.
- Archived stubs may summarize provenance and link Git history; they must not
  contain executable prompts, commands, TODO sequences, or imperative roadmaps.
- Never mirror Project Queue, Code Review, PR, or slice state in documentation.

## Required Checks

Run:

```bash
python3 scripts/check_docs_contract.py
python3 scripts/check_strategy_claims.py
cd docs && npm run typecheck && npm run build
```

The contract checker enforces canonical ownership, the mirrored public use-case
block, public help links, capability/document status separation, ADR structure,
sole root-route ownership, sidebar placement, and non-operational archive stubs.
Docusaurus treats broken links as build failures.

## Historical Context

The previous master roadmap and parity program remain discoverable only as
historical summaries. Full content is preserved in Git history and closed issue
[#475](https://github.com/seraph-quest/seraph/issues/475). They are not active
instructions or target owners.
