# 13. Trusted Code Plugins RFC

## Status

- accepted as a negative decision for the current architecture
- trusted code plugins are **not** part of Seraph's extension platform
- revisit only through a new explicit RFC after the extension platform is fully shipped and operating in production

## Decision

Seraph will **not** add a general trusted-code plugin runtime as part of the current extension-platform transition.

The supported architecture remains:

- typed declarative capability packs
- connector packs
- MCP connectors
- managed first-party connectors
- bundled native tools that remain core-owned and repo-shipped

Trusted third-party or user-installed code plugins are deferred.

## Why This Decision Is Correct Now

### 1. The current extension platform already covers the main leverage surfaces

Seraph already has shipped or explicitly typed extension paths for the main leverage surfaces:

- skills
- workflows
- runbooks
- starter packs
- provider presets
- MCP connectors
- the typed managed-connector, observer, and channel seams the transition program is still finishing

That means the product can keep growing through typed packages and connector paths without granting arbitrary in-process code execution to external packages.

### 2. Trusted code plugins would collapse the trust model too early

Seraph's current architecture depends on core-owned boundaries:

- policy
- approvals
- audit and activity
- secret handling
- session and thread state
- workflow execution
- capability preflight and repair
- model routing

A broad trusted-code plugin runtime would immediately pressure those boundaries by introducing packages that can:

- run arbitrary code inside the host
- bypass typed contribution contracts
- expand the attack surface faster than the current lifecycle and audit UX is designed to handle

### 3. The highest-value problems are elsewhere

The missing work is not "arbitrary code plugins." The missing work is:

- better packaged authoring ergonomics
- stronger lifecycle UI
- better connector health and repair
- smoother install/update/remove flows
- more complete observer/channel packaging

Those are all extension-platform completion problems, not trusted-code-plugin problems.

### 4. MCP plus managed connectors already cover the integration story better

When Seraph needs external reach:

- MCP handles broad long-tail integration
- managed connectors handle high-value curated integrations

That is a better split than inventing a second broad general-purpose plugin runtime now.

## What Still Counts As Supported

The RFC does **not** remove these supported seams:

- bundled native tools in `backend/src/tools`
- manifest-backed capability packs
- connector packs
- MCP integrations
- managed first-party connectors

Important clarification:

- bundled native tools are not "plugins" in this RFC
- they remain core-shipped trusted code owned by the Seraph repository

## What Is Explicitly Out Of Scope

The current platform does **not** support:

- marketplace-installed arbitrary Python packages running inside the backend
- third-party code plugins with unrestricted host access
- extension packages that register new privileged runtime hooks directly into policy, approvals, audit, or routing
- "just run this repo as a plugin" installation flows

## Revisit Criteria

Seraph should only reopen this decision if the typed extension platform proves insufficient after the rest of the architecture is complete.

A new RFC should only be considered if all of the following are true:

1. typed capability packs and connector packs are fully shipped and broadly exercised
2. MCP and managed connectors demonstrably fail to cover a high-value class of integrations
3. there is a concrete product case that cannot be handled by declarative extensions, connectors, or bundled core code
4. extension lifecycle, audit, approval, and policy UX are already mature enough to expose code-plugin risk clearly

## Minimum Safety Bar If Reopened Later

If Seraph ever revisits trusted code plugins, the baseline should be much narrower than "run arbitrary code."

The minimum acceptable bar would include:

- explicit trust tier distinct from normal extensions
- strong package provenance and signing requirements
- operator-facing install review and approval
- isolated execution model or process boundary where feasible
- restricted host API instead of full in-process object access
- lifecycle audit for install, update, enable, disable, execution, and failures
- permissions mapped into policy and approvals
- clear disable/remove/recovery path

If that bar cannot be met, Seraph should keep the current negative decision.

## Product Consequences

This RFC means:

- extension-platform execution continues to focus on typed contributions
- docs and tooling should teach capability packs and connectors, not generic code plugins
- roadmap items should not casually introduce new "plugin" language for privileged runtime code
- future reach should first prefer connector-pack, MCP, or managed-connector designs

## Final Outcome

For the current architecture, the answer is:

- **no general trusted-code plugin runtime**
- **yes to typed extension packages**
- **yes to MCP as one connector path**
- **yes to managed connectors for curated high-trust integrations**
- **yes to bundled core-owned native tools**

That keeps Seraph extensible without weakening the trust model before the platform is ready.
