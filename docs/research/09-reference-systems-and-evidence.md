# 09. Reference Systems And Evidence

## Purpose

This file defines how competitive and architectural claims are allowed to enter the Seraph docs.

The rule is simple: if a claim affects roadmap priority, product positioning, or a “we are better/worse than X” statement, it needs a source.

## Allowed Evidence Types

Use these in descending order of strength:

1. direct inspection of the Seraph repo
2. official reference-system docs and official repos
3. primary research papers and ArXiv
4. clearly labeled vendor/product docs for interface references

If a claim cannot be supported by one of those, it should be marked `Unknown`.

## Disallowed Shortcuts

- social-media lore as product truth
- benchmark claims without a linked source
- “everybody knows” comparisons
- smoothing uncertainty into confident prose

## Comparison Method

When comparing Seraph to OpenClaw, Hermes, or IronClaw:

- compare against Seraph’s shipped surface on `develop`
- compare only on explicit benchmark axes
- label each axis `Ahead`, `At Par`, `Behind`, or `Unknown`
- give a short why plus one or more source links

## Verified Source Registry

### Local repo truth

- Seraph repo under `/Users/bigcube/Desktop/repos/seraph`
- local MAAS docs under `/Users/bigcube/Desktop/repos/maas/docs`

### Official reference-system docs reviewed for this pass

- [OpenClaw docs](https://docs.openclaw.ai/)
- [OpenClaw GitHub](https://github.com/openclaw/openclaw)
- [Hermes docs](https://hermes-agent.nousresearch.com/)
- [Hermes GitHub](https://github.com/NousResearch/hermes-agent)
- [IronClaw site](https://www.ironclaw.com/)
- [IronClaw GitHub](https://github.com/nearai/ironclaw)

### Verified interface references

- [Godel Terminal docs](https://docs.godelterminal.com/)
- [OpenBB Workspace docs](https://docs.openbb.co/workspace)
- [Quantower DOM Surface docs](https://help.quantower.com/quantower/analytics-panels/dom-surface)
- [SpreadFighter](https://spreadfighter.com/) and [Scalper.Ai](https://spreadfighter.com/scalperai)

### Primary research sources used in this pass

- [MemoryBank](https://arxiv.org/abs/2305.10250)
- [MemGPT](https://arxiv.org/abs/2310.08560)
- [Generative Agents](https://arxiv.org/abs/2304.03442)
- [LoCoMo](https://arxiv.org/abs/2402.17753)
- [LongMemEval](https://arxiv.org/abs/2410.10813)
- [HACO](https://arxiv.org/abs/2202.10341)
- [AgentBench](https://arxiv.org/abs/2308.03688)
- [SWE-bench](https://arxiv.org/abs/2310.06770)
- [OSWorld](https://arxiv.org/abs/2404.07972)
- [τ-bench](https://arxiv.org/abs/2406.12045)
- [GAIA](https://arxiv.org/abs/2311.12983)
- [Mixed-initiative UI principles](https://www.microsoft.com/en-us/research/publication/principles-mixed-initiative-user-interfaces/)
- [Attention-sensitive alerting](https://www.microsoft.com/en-us/research/publication/attention-sensitive-alerting/)

## Known Unknowns

The current official materials for OpenClaw, Hermes, and IronClaw do not provide enough evidence to score every axis confidently.

Known areas where caution is required:

- intervention quality measured over time
- published runtime-eval rigor beyond tests/diagnostics
- real-world outcome quality for self-healing and autonomous routines

Those should stay `Unknown` until stronger official evidence appears.

## Implication For The Docs

The research tree should now do two things at once:

- define the target Seraph product
- show exactly where that target comes from and where the evidence is still thin

That keeps the roadmap anchored to verifiable gaps rather than taste alone.

The implementation tree should mirror that work explicitly:

- research evidence rules should have an implementation-side docs contract mirror
- benchmark logic in research should have a benchmark-status mirror in implementation
- superiority-program logic in research should have a delivery mirror in implementation
- active execution state should live in GitHub, not as a stale duplicate in research
