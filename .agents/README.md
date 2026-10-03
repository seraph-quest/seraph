# Agent Team Protocol

Seraph uses Codex as team lead for substantial planning, architecture,
implementation, review, and runtime work. `AGENTS.md` is the authority for repo
rules; this directory holds reusable packets and review artifact formats.

Use this execution packet for delegated work:

```text
Role:
Owner:
Scope:
Files/modules:
Non-goals:
Acceptance criteria:
Proof required:
Expected output:
Timeout/fallback:
```

Use this handoff format for substantial work:

```text
Goal:
Branch/base:
Linked issue:
Files changed:
Verification:
Evidence:
Critic disposition:
Open risks:
Next step:
```

No role may claim an issue, project field, PR, test, runtime probe, or docs
update happened unless tool output confirmed it.

## Durable Work And Recovery

Create agent worktrees inside the repository's ignored `.agent-worktrees/`
directory. Keep handoffs, source manifests and retained validation evidence in
the ignored `.agent-evidence/` directory, with private permissions when they
contain operator or workspace data. Do not use `/tmp` for worktrees or retained
evidence. Disposable test fixtures may use a private temporary directory when
the runtime's trusted-path contract requires it; copy required receipts into
the durable evidence directory before cleanup.

Checkpoint related source changes on the owned feature branch frequently.
Record the exact commit, base, file ownership and verification in each handoff.
The lead should push recoverable source checkpoints to the owned branch;
private artifacts, credentials and local runtime data must remain excluded.
Local ignored directories survive temporary-directory cleanup but are not a
backup. Pushed source checkpoints provide a separate recovery copy; private
evidence still requires the operator's workspace backup.

After an interruption, verify the surviving branches, source and evidence
before resuming. Missing receipts must be rerun, and missing uncommitted edits
must be recovered or reconstructed. A checkpoint is not independent review,
milestone completion or permission to open a partial PR.
