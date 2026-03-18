# Workstream 05: Guardian Intelligence

## Status On `develop`

- [ ] Workstream 05 is only partially shipped on `develop`.

## Paired Research

- primary design docs: [01. Guardian Thesis](/research/guardian-thesis), [02. Human Model And Memory](/research/human-model-and-memory), and [11. Superiority Program](/research/superiority-program)
- synthesis context: [00. Research Synthesis](/research)

## Shipped On `develop`

- [x] soul-backed persistent identity
- [x] long-term vector memory and consolidation
- [x] hierarchical goals and progress tracking
- [x] strategist agent with restricted guardian tool set
- [x] daily briefing, evening review, activity digest, and weekly activity review foundations
- [x] observer-driven user-state and attention-budget modeling
- [x] explicit guardian-state synthesis that unifies observer context, memory, current session, recent sessions, and confidence for downstream agent paths
- [x] explicit intervention policy that distinguishes act, bundle, defer, request-approval, and stay-silent outcomes for proactive guardian messages
- [x] persisted guardian intervention records and explicit user-feedback capture that flow back into guardian-state summaries

## Working On Now

- [x] this workstream is now the repo-wide active focus after the first runtime baseline shipped
- [x] this workstream has now shipped the `guardian-feedback-loop` foundation on this branch
- [x] this workstream owns `observer-salience-and-confidence-model` in the master 10-PR queue

## Still To Do On `develop`

- [ ] richer human world modeling that goes beyond current retrieval plus heuristics
- [ ] stronger learning loops based on intervention outcomes instead of only storing them
- [ ] observer salience and confidence modeling for better prioritization and interruption quality
- [ ] stronger linkage between guardian state, execution choices, and feedback-driven policy adaptation

## Non-Goals

- marketing “guardian intelligence” before the learning loop is real
- confusing retrieval volume with understanding quality

## Acceptance Checklist

- [x] Seraph can retain identity, memory, and goals across sessions
- [x] Seraph can generate proactive guardian outputs from that context
- [x] Seraph has an explicit guardian-state object rather than spreading that reasoning across call sites
- [x] Seraph has an explicit intervention policy rather than only deliver-versus-queue heuristics
- [x] Seraph records intervention outcomes and explicit user feedback in durable guardian state
- [ ] Seraph learns from intervention outcomes in a way that changes future policy
- [ ] Seraph reliably models the human well enough to intervene at consistently high quality
