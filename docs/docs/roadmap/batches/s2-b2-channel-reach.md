---
title: S2-B2 Channel Reach
---

# S2-B2: Channel Reach

## Intent

Add the first serious external communication channel so Seraph can reach the user where they already are.

## Capabilities in scope

- one initial channel, likely Telegram first
- inbound and outbound session routing
- proactive delivery to that channel
- lightweight message normalization and user/session mapping

## Non-goals

- simultaneous launch across many channels
- complex group chat policies beyond the minimum safe path
- enterprise team routing

## Required architectural changes

- define a channel adapter abstraction
- route messages into the existing session system
- support outbound proactive delivery through channel preferences
- add enough policy to prevent spammy or unsafe delivery behavior

## Likely files/systems touched

- backend channel integration layer
- session identity and routing
- proactive delivery paths
- settings and configuration UX

## Acceptance criteria

- Seraph can receive and answer messages on one external channel
- proactive messages can reach the user there
- channel conversations map cleanly into Seraph sessions
- the browser UI remains the canonical rich interface rather than being broken by channel work

## Dependencies on earlier batches

- depends on [S2-B1 Native Presence](./s2-b1-native-presence) or equivalent presence groundwork
- depends on Season 1 reliability so channel delivery is credible

## Open risks

- multi-surface behavior can become confusing without clear session identity rules
- notifications may become noisy if interruption logic is not respected across channels
- channel-specific auth and rate limits can add operational drag
