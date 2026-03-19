---
title: S2-B3 Ambient Guardian
---

# S2-B3: Ambient Guardian

## Intent

Improve how Seraph shows up between major interactions so its presence feels intentional, calm, and useful.

## Capabilities in scope

- richer ambient summaries and nudges
- better notification/report surfaces
- stronger timing and delivery polish outside the core chat panel
- clearer transitions between ambient, nudge, advisory, and alert moments

## Non-goals

- full Season 4 embodiment work
- complicated gamification systems
- every possible delivery surface

## Required architectural changes

- unify proactive delivery behavior across desktop and external channels
- improve surface selection for different intervention types
- tighten summary/report formatting for briefings, reviews, and queued bundles
- expose more of the guardian state outside direct chat

## Likely files/systems touched

- observer and delivery logic
- frontend ambient surfaces
- desktop or channel notification formatting
- daily briefing / review presentation paths

## Acceptance criteria

- proactive delivery feels more deliberate and less like raw chat output
- users can distinguish ambient presence from urgent intervention
- briefings, reviews, and queued insights read well on the active surfaces
- this batch sets up Season 4 rather than duplicating it

## Dependencies on earlier batches

- depends on [S2-B1 Native Presence](./s2-b1-native-presence)
- depends on [S2-B2 Channel Reach](./s2-b2-channel-reach) if external channels are part of the chosen delivery mix

## Open risks

- surface proliferation can fragment the experience
- better delivery without better intelligence can make Seraph feel louder rather than better
- batch boundaries can blur with Season 4 if embodiment work starts too early
