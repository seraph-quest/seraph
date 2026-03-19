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
- [x] first multi-signal outcome-learning loop that uses recent outcomes on the same intervention type to reduce interruptions after negative feedback and prefer direct delivery or native reroute after repeated positive/acknowledged outcomes
- [x] second-layer salience calibration that promotes aligned active-work signals and allows grounded high-salience nudges to bypass generic high-interruption bundling outside focus mode
- [x] deeper guardian behavioral eval coverage that proves grounded high-salience delivery versus degraded-confidence defer behavior at the delivery gate
- [x] deeper guardian behavioral eval coverage that proves strategist tick can combine learned delivery bias, native delivery, and continuity-state visibility in one deterministic contract
- [x] guardian world model that now carries current focus, active commitments, active projects, active constraints, recurring patterns, active routines, memory signals, continuity threads, open loops or pressure, recent execution pressure, focus alignment, and intervention receptivity inside guardian state
- [x] guardian state now also carries learned communication guidance derived from recent intervention outcomes, including timing and blocked-state bias, instead of only raw outcome history

## Working On Now

- [x] this workstream remains central in the repo-wide horizon through stronger learning quality after the new structured world-model fusion pass shipped
- [x] the `observer-salience-and-confidence-model` foundation is now shipped on `develop`
- [x] the first multi-signal learning layer and first salience-calibration pass are now shipped, and the next major gap is deeper modeling plus richer long-horizon learning rather than more missing observer fields
- [x] `world-model-memory-fusion-v5`, `guardian-learning-policy-v5`, and `guardian-behavioral-evals-v5` are now shipped on this branch, so the next gap shifts to deeper durable modeling and stronger policy learning rather than more missing first-pass structure

## Still To Do On `develop`

- [ ] richer human world modeling that goes beyond the new project/routine/constraint/pattern/continuity-aware world-model layer
- [ ] stronger learning loops based on intervention outcomes beyond the first multi-signal delivery/channel/escalation plus phrasing/cadence/timing/blocked-state layer
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
- [x] Seraph has deterministic behavioral proof that strategist nudges can follow learned native-delivery bias and still remain visible through continuity surfaces
- [x] Seraph has a first explicit world model inside guardian state instead of relying only on retrieval plus prompt prose
- [x] Seraph's world model now reflects recent active projects, active constraints, recurring patterns, active routines, structured memory signals, continuity threads, and degraded execution signals instead of only static focus/commitment text
- [x] Seraph now feeds learned communication guidance back into guardian state and intervention policy instead of leaving recent outcomes as passive history
- [ ] Seraph reliably learns from intervention outcomes in a way that improves future policy quality beyond the first delivery/channel bias layer
- [ ] Seraph reliably models the human well enough to intervene at consistently high quality
