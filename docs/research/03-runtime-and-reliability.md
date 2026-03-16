# 03. Runtime And Reliability

## Goal

Seraph needs a runtime that degrades gracefully and stays explainable under failure.

## Design Direction

The runtime should support:

- multiple provider targets
- ordered fallbacks
- policy-aware target selection
- local routing where bounded tasks benefit from it
- broad runtime audit visibility
- deterministic regression-style evals

## Why This Matters

A guardian product cannot disappear or become opaque the first time a provider, integration, or helper path fails. Reliability is product quality here, not a backend nicety.

## Research Priorities

- richer provider selection policy, not just explicit per-path overrides
- better rules for when local models should be preferred
- eval coverage that reaches beyond seams into more end-to-end behavioral confidence
- stronger operator visibility into why a route or fallback was chosen

## Success Condition

The human should not need to understand the full runtime graph to trust that Seraph handled failure sanely.
