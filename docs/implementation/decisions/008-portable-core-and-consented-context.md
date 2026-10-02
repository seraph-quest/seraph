---
title: "ADR-008: Portable Core And Consented Context"
---

# ADR-008: Portable Core And Consented Context

**Status:** Accepted

**Decision class:** Target architecture

**Tracked work:** [#926](https://github.com/seraph-quest/seraph/issues/926), under
[#899](https://github.com/seraph-quest/seraph/issues/899)

**Supersedes:** [ADR-004](./004-gpu-core-mac-edge-topology.md) for fixed host
placement only; its pairing, consent, revocation, authenticated transport and
canonical-state safeguards remain applicable.

## Context

The operator requires Seraph to run on macOS and Linux alike. ADR-004 fixed the
target core on a GPU host and assigned the Mac an edge-only role. ADR-006 already
removes local GPU/model services as active inference prerequisites. A deployment
example must not prevent an operator from choosing either supported host OS.

## Decision

macOS and Linux are peer core-host targets. The operator chooses one canonical
workspace host. The browser cockpit, backend, storage, durable jobs, approvals,
memory and governed OpenRouter route use the same authority and recovery
contracts on either host. `jupyter` is a historical deployment example, not a
required machine or OS. An optional paired device never gains canonical
authority; the core host may also supply explicitly permitted local context.

Core startup and settings cannot depend on OS-specific observation or execution
services. Resolve native APIs lazily behind a selected adapter/profile, and show
actual readiness, blocked/degraded state and recovery. Unsupported optional
profiles must fail closed without silently changing the approved mechanism.
Linux process-supervisor proof cannot establish macOS execution support.

Selected desktop context requires deliberate selection, local preview/redaction
and explicit send. Before implementation, each selected capture adapter must
have a reviewed, observable permission and protected-surface contract. An
adapter unable to satisfy that contract remains blocked. Do not substitute
generic file upload or claim automatic password exclusion from an OS capture
API alone. No background capture or cloud-analysis consent follows from send.

Accept reviewed content through a typed task-attachment contract carrying the
authenticated original owner/root, exact active task and goal revisions, source
identity/revision, content digest, bounded size, expiry and idempotency. Reuse
existing pairing authentication and private artifact/evidence seams. Provide
task-scoped readback, deletion/revocation, provenance and explicit no_learning.
This path must not create general ScreenObservation rows or admit attachments
to automatic analysis. Any later model analysis needs its own current grant,
finite budget and governed admission. Existing general observation retains its
separate purpose and consent contract.

Offline preview stays local until explicit send. Revocation, deletion, expiry
or changed owner/task/goal bindings reject stale uploads and retries. Pairing
authenticates transport and never grants execution or data-egress authority.
Canonical host moves retain the existing authenticated maintenance fence,
backup/restore, accounting continuity and authority invalidation requirements.
SSH remains administration only; runtime traffic uses documented APIs.

## Consequences

- Shared core portability and each optional adapter's readiness are separate
  observable claims. Existing Mac-native code is not retroactively portable.
- No always-on Linux service, Mac edge, GPU, model wrapper, or native capture
  process is a prerequisite for the core.
- Shipped support still requires the owning implementation and actual receipts;
  this target does not claim new platform or capture behavior is Shipped.

## Verification

Prove shared launcher/core/settings and owner-scoped UI/API behavior with focused
checks and managed runtime receipts. Record actual macOS and Linux execution
separately; mocks or imports cannot establish platform support. Prove a genuine
selected-context preview/send/artifact/readback journey using the selected
adapter, plus signed paired transport where applicable. Test permission denial,
protected input, wrong/stale authority, malformed/oversized/tampered content,
expiry, offline revoke/delete/retry races and non-resurrection. Verify that
attachment acceptance creates no observation-analysis eligibility or model
request. Missing device evidence is reported, not invented, and does not block
independent shared core work.
