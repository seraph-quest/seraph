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
- [x] observer salience, confidence, and interruption-cost scoring that feeds guardian state and proactive policy
- [x] explicit guardian-state synthesis that unifies observer context, memory, current session, recent sessions, confidence, and observer salience signals for downstream agent paths
- [x] explicit intervention policy that distinguishes act, bundle, defer, request-approval, and stay-silent outcomes for proactive guardian messages, including low-salience suppression and high-interruption bundling
- [x] persisted guardian intervention records and explicit user-feedback capture that flow back into guardian-state summaries
- [x] first outcome-learning loop that uses recent negative feedback on the same intervention type to reduce future interruption eagerness for similar low-urgency nudges
- [x] second-layer salience calibration that promotes aligned active-work signals and allows grounded high-salience nudges to bypass generic high-interruption bundling outside focus mode
- [x] deeper guardian behavioral eval coverage that proves grounded high-salience delivery versus degraded-confidence defer behavior at the delivery gate
- [x] first explicit human/world model that carries current focus, active commitments, open loops or pressure, focus alignment, and intervention receptivity inside guardian state

## Working On Now

- [x] this workstream remains central in the repo-wide horizon through stronger learning quality and a deeper second world-model pass after the first explicit focus or commitments layer shipped
- [x] the `observer-salience-and-confidence-model` foundation is now shipped on `develop`
- [x] the first feedback-driven learning layer and first salience-calibration pass are now shipped, and the next major gap is deeper modeling plus richer multi-signal learning rather than more missing observer fields

## Still To Do On `develop`

- [ ] richer human world modeling that goes beyond the first explicit focus, commitments, pressure, and receptivity layer
- [ ] stronger learning loops based on intervention outcomes beyond the first negative-feedback interruption bias
- [ ] stronger salience calibration and confidence quality beyond the first aligned-work/high-salience pass
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
- [x] Seraph learns at least one policy-relevant lesson from intervention outcomes and explicit user feedback
- [x] Seraph scores observer state by salience, confidence, and interruption cost before guardian strategy and delivery
- [x] Seraph uses calibrated high-salience observer signals to change real delivery outcomes instead of only logging them
- [x] Seraph has deterministic behavioral proof that the calibrated high-salience deliver path and degraded-confidence defer path stay distinct at the delivery gate
- [x] Seraph has a first explicit world model inside guardian state instead of relying only on retrieval plus prompt prose
- [ ] Seraph reliably learns from intervention outcomes in a way that improves future policy quality beyond the first bias layer
- [ ] Seraph reliably models the human well enough to intervene at consistently high quality
