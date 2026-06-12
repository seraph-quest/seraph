# Full Production Parity Goal Prompt

Use this prompt with the goal command when the objective is to complete the full production-grade Seraph parity and targeted-exceedance program tracked by parent issue [#475](https://github.com/seraph-quest/seraph/issues/475).

```text
Goal: complete the post-DP implementation gap-closure train needed to make production-grade feature-parity and targeted-exceedance claim-readiness possible for Seraph against Hermes, OpenClaw, and IronClaw, while preserving Seraph's guardian-workspace vision. Do not stop at planning. Complete the implementation train, board updates, PRs, reviews, docs, validation, and final claim-readiness gate.

Ground rules:
- Follow AGENTS.md exactly.
- Never commit directly to develop or main.
- Use feat/ or fix/ branches only.
- Ready PRs only; no draft PRs.
- Use parent issue #475 as the execution parent.
- Do not create a new parent roadmap issue.
- Do not reopen closed M0-M9, #476-#482, #491-#497, #505-#512, #522-#530, #540-#547, or #557-#564 work.
- Treat #573-#580 as the active post-DP implementation gap-closure train.
- Keep docs as shipped/strategic truth; GitHub issues, PRs, and the Project board are the live execution layer.
- Do not claim production readiness, full parity, superiority, secure/private-by-default execution, IronClaw-class execution, OpenClaw-class reach, solved operator control, solved learning, production-secure marketplace, safe autonomous browser/computer-use, full browser parity, memory superiority, or reference-system exceedance unless the claim ledger permits exact wording after merged proof.

Team model:
- Act as team lead.
- Create or update a role-fit team before each substantial batch.
- Required roles across the train: Planner, Repo Explorer, Runtime Reliability Worker, Security/Trust Worker, Reach Worker, Guardian/Memory Worker, Operator UX Worker, Marketplace Worker, Browser/Execution Worker, Docs/Board Integrator, and independent Critic/Contrarian.
- Delegate bounded implementation slices with explicit ownership, acceptance criteria, proof requirements, and expected output.
- Run an independent Critic/Contrarian pass before each PR and before any issue/board/claim-finalization action.
- Verify subagent claims before using them in docs, issue updates, PR bodies, commits, or final status.

Execution batches:
1. Complete #573 / Batch DQ: post-DP durable orchestration gap-closure implementation.
   - Implement real multi-session, multi-agent, scheduler interruption, crash/retry, idempotency, side-effect reconciliation, and operator recovery improvements.
   - Add operator-visible receipts, benchmark-proof visibility, unsafe-resume blocks, duplicate-side-effect suppression, and false-claim scanning.

2. Complete #574 / Batch DR: post-DP secure capability-host gap-closure implementation.
   - Harden runtime profile selection, deny-by-default egress, scoped credentials, package/browser/session boundaries, hostile chains, quarantine, revocation, and recovery authority.
   - Add redaction, denial, hostile-chain, credential-broker, isolation, recovery, and false-claim proof.

3. Complete #575 / Batch DS: post-DP reach and channel gap-closure implementation.
   - Improve selected reach surfaces with pairing, revocation, consent, rate-limit/abuse handling, provider outage, offline/degraded recovery, continuity, and voice/media receipt surfaces where implemented.
   - Preserve Seraph's selective guardian-aware reach vision rather than chasing raw channel count.

4. Complete #576 / Batch DT: post-DP guardian learning and memory gap-closure implementation.
   - Implement consented long-horizon learning, behavior-change ablations, reversible learning deltas, rollback, stale evidence decay, provider quarantine, delete/export propagation, and safety monitoring.
   - Keep memory and guardian claims evidence-bound and privacy-safe.

5. Complete #577 / Batch DU: post-DP operator debugging and recovery-control gap-closure implementation.
   - Make long-work operation dense and recoverable: inspect, pause, resume, retry, repair, branch, compare, revoke, quarantine, handoff, rollback, audit, search, replay, and runbook flows.
   - Include stale approval blocking, safe denial, authority transfer, audit integrity, accessibility/keyboard proof, and operator effort evidence.

6. Complete #578 / Batch DV: post-DP capability marketplace lifecycle gap-closure implementation.
   - Implement safer install, update, downgrade, disable, rollback, quarantine, re-entry, provenance, signature, publisher trust, SBOM/dependency, vulnerability, compatibility, permission, and runtime-boundary diagnostics.
   - Integrate marketplace lifecycle with secure capability-host policy and operator audit receipts.

7. Complete #579 / Batch DW: post-DP browser/computer-use reliability gap-closure implementation.
   - Improve selected provider modes, live degradation handling, credentialed test-account recovery, partitioned session/profile/cookie/credential/file/network/private-data boundaries, site drift recovery, and hostile-page fail-closed behavior.
   - Keep dangerous-action denial and existing-session trust boundaries explicit.

8. Complete #580 / Batch DX only after #573-#579 are closed with Project fields Status=Done, PR=Merged, and Code Review=Passed.
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

Validation discipline:
- Do not wait on the full test suite before implementing the selected batch features.
- Implement all features in the active batch first.
- Then run focused tests, benchmark suites, operator API checks, false-claim scans, docs checks, and git diff --check.
- Fix failing tests after implementation and before PR readiness.
- PR bodies must include scope, validation, team roles, Critic/Contrarian disposition, linked issue, Project/board receipt, and blocked-claim posture.

Completion definition:
- The goal is not complete when issues are merely created or docs are updated.
- The goal is complete only when #573-#580 are implemented, merged, board-reconciled, docs and claim ledger agree with shipped truth, current competitor sources are refreshed, false-completion scans pass, and DX permits exact final wording.
```
