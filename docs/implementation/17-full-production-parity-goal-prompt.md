# Full Production Parity Goal Prompt

Use this prompt with the goal command when the objective is to complete the full production-grade Seraph parity and targeted-exceedance program tracked by parent issue [#475](https://github.com/seraph-quest/seraph/issues/475).

This prompt is intentionally feature-only until parity is reached. The goal is not to produce more proof surfaces as the main work, and it is not to pair each feature with a minimal proof companion task. The goal is to ship the user-facing parity capabilities Seraph still lacks and make those capabilities coherent with the guardian-workspace vision. Receipts, expanded tests, claim-ledger updates, and docs happen only after the full feature-parity implementation is complete, as a separate honesty and claim-readiness pass.

```text
Goal: complete the post-DP implementation gap-closure train needed to make production-grade feature parity and targeted exceedance possible for Seraph against Hermes, OpenClaw, and IronClaw, while preserving Seraph's guardian-workspace vision. Work feature-only first: implement the operator-facing capabilities, workflows, controls, runtime behavior, recovery paths, channels, memory behavior, security behavior, marketplace flows, and browser/computer-use behavior before adding proof wrappers, expanded docs, or claim-readiness layers. Do not stop at planning. Complete the feature implementation train first, then run the separate honesty and claim-readiness pass.

Ground rules:
- Follow AGENTS.md exactly.
- Never commit directly to develop or main.
- Use feat/ or fix/ branches only.
- Ready PRs only; no draft PRs.
- Use parent issue #475 as the execution parent.
- Do not create a new parent roadmap issue.
- Do not reopen closed M0-M9, #476-#482, #491-#497, #505-#512, #522-#530, #540-#547, #557-#564, or #573-#580 work.
- Treat #573-#580 as closed bounded-proof/history, not active feature work. Before implementation, rescope the open post-DX board batches or create new huge PR-sized linked issues as a feature-only train.
- Keep docs as shipped/strategic truth; GitHub issues, PRs, and the Project board are the live execution layer.
- Prioritize user-facing feature work exclusively until the parity implementation train is complete. For every batch, identify the concrete user/operator experience that improves: the screen, command, API, workflow, control, recovery path, channel, memory behavior, security behavior, marketplace flow, or browser action that becomes better for a real user.
- Do not add new proof surfaces, proof-only endpoints, proof-only benchmark suites, expanded docs, claim-ledger changes, or per-batch proof wrappers inside feature batches.
- Do not create proof-only successor batches while material parity capabilities remain unimplemented.
- After the full feature train is implemented, run one separate claim-readiness phase for receipts, focused tests, docs, board reconciliation, source refresh, and claim-ledger wording.
- Do not claim production readiness, full parity, superiority, secure/private-by-default execution, IronClaw-class execution, OpenClaw-class reach, solved operator control, solved learning, production-secure marketplace, safe autonomous browser/computer-use, full browser parity, memory superiority, or reference-system exceedance unless the claim ledger permits exact wording after merged proof.

Team model:
- Act as team lead.
- Create or update a role-fit team before each substantial batch.
- Required roles across the train: Planner, Repo Explorer, Runtime Reliability Worker, Security/Trust Worker, Reach Worker, Guardian/Memory Worker, Operator UX Worker, Marketplace Worker, Browser/Execution Worker, Docs/Board Integrator, and independent Critic/Contrarian.
- Delegate bounded implementation slices with explicit ownership, user-facing acceptance criteria, and expected output.
- Run an independent Critic/Contrarian pass before each PR and before any issue/board/claim-finalization action.
- Verify subagent claims before using them in docs, issue updates, PR bodies, commits, or final status.

Execution batches:
1. Durable orchestration feature batch.
   - Implement real multi-session, multi-agent, scheduler interruption, crash/retry, idempotency, side-effect reconciliation, and operator recovery improvements.
   - User-facing first: make long-running work easier to resume, inspect, recover, and trust after interruption.

2. Secure capability-host feature batch.
   - Harden runtime profile selection, deny-by-default egress, scoped credentials, package/browser/session boundaries, hostile chains, quarantine, revocation, and recovery authority.
   - User-facing first: make risky tool, connector, browser, package, and credential paths visibly safer and easier to recover when blocked or quarantined.

3. Reach and channel feature batch.
   - Improve selected reach surfaces with pairing, revocation, consent, rate-limit/abuse handling, provider outage, offline/degraded recovery, continuity, and voice/media handoff behavior where implemented.
   - Preserve Seraph's selective guardian-aware reach vision rather than chasing raw channel count.
   - User-facing first: make the selected channels genuinely usable for daily interruption, approval, recovery, and continuity.

4. Guardian learning and memory feature batch.
   - Implement consented long-horizon learning, behavior-change ablations, reversible learning deltas, rollback, stale evidence decay, provider quarantine, delete/export propagation, and safety monitoring.
   - Keep memory and guardian claims evidence-bound and privacy-safe.
   - User-facing first: make Seraph's memory visibly improve decisions, restraint, recall, correction, and provider conflict handling for the operator.

5. Operator debugging and recovery-control feature batch.
   - Make long-work operation dense and recoverable: inspect, pause, resume, retry, repair, branch, compare, revoke, quarantine, handoff, rollback, audit, search, replay, and runbook flows.
   - User-facing first: make the cockpit feel like a real control surface for serious work, not a report viewer.

6. Capability marketplace lifecycle feature batch.
   - Implement safer install, update, downgrade, disable, rollback, quarantine, re-entry, provenance, signature, publisher trust, SBOM/dependency, vulnerability, compatibility, permission, and runtime-boundary diagnostics.
   - User-facing first: make installing and maintaining capabilities understandable, reversible, diagnosable, and safe for an operator.
   - Integrate marketplace lifecycle with secure capability-host policy only where needed for the actual user-facing lifecycle flow.

7. Browser/computer-use reliability feature batch.
   - Improve selected provider modes, live degradation handling, credentialed test-account recovery, partitioned session/profile/cookie/credential/file/network/private-data boundaries, site drift recovery, and hostile-page fail-closed behavior.
   - Keep dangerous-action denial and existing-session trust boundaries explicit.
   - User-facing first: make browser/computer-use runs more reliable, explainable, recoverable, and bounded when sites drift or credentials/sessions are involved.

8. Claim-readiness phase only after the feature train has shipped.
   - Refresh current Hermes/OpenClaw/IronClaw sources with URLs and access dates.
   - Reconcile issues, PRs, Project fields, tests, docs, operator endpoints, benchmark proof, and claim ledger.
   - Run local and GitHub false-completion scans.
   - Run independent Critic/Contrarian no-block review.
   - Permit only exact claim-ledger wording and keep every unsupported broad claim blocked.

Project board contract:
- When a batch starts, set Queue=Now and Status=In Progress.
- For open aggregate PRs, link the PR to the parent batch issue and set PR=Open, Code Review=Pending or Running as appropriate.
- When review passes, set Code Review=Passed.
- When the PR merges, set PR=Merged and Status=Done.
- Keep child slice issues only when separate ownership, blocker status, or separate reprioritization makes them necessary.

Feature-first discipline:
- Do not wait on the full test suite before implementing the selected batch features.
- Implement all user-facing features in the active batch first.
- Do not turn a feature batch into a feature-plus-minimal-proof batch.
- Do not spend feature-batch time building proof scaffolding, benchmark harnesses, claim scans, broad docs reconciliation, or claim-ledger changes.
- Run only basic local build/smoke sanity when packaging a PR, and keep that work subordinate to the feature implementation rather than treating it as parity evidence.
- Fix failures caused by the changed feature surface before PR readiness.
- PR bodies must include scope, user-facing behavior shipped, team roles, Critic/Contrarian disposition, linked issue, Project/board receipt, and blocked-claim posture. They must not claim parity proof or claim-readiness during the feature-only phase.

Completion definition:
- The goal is not complete when issues are merely created or docs are updated.
- The feature phase is complete when the post-DX feature-only train ships its intended user-facing parity capabilities and is merged.
- The overall goal is complete only after the separate DX claim-readiness phase reconciles board state, docs, tests, receipts, current competitor sources, false-completion scans, and exact claim-ledger wording.
```
