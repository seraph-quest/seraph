# Open PRs and how to land them

Both PRs are approved by Fable 5.2 high. Each has had develop (with the #1268
lifecycle fix) merged in and pushed. CI was running when this was written.

| PR | Branch / worktree | Head | Review |
|---|---|---|---|
| #1265: screenshot cap (#1263) | `fix/1263-screenshot-cap` / `.agent-worktrees/fix-1263-screenshot-cap` | `da80bda7` | Fable APPROVE after two rounds |
| #1267: GitHub activity source, slice 1 of #1264 | `feat/1264-gh-activity-source` / `.agent-worktrees/feat-1264-gh-activity-source` | `3eecd3d9` | Fable APPROVE after three rounds |

## Steps

1. Check CI:
   ```bash
   gh pr checks 1265
   gh pr checks 1267
   ```
   Every job must pass. `quarantine-lane` skipping is fine. Shard 8 runs close to
   its 40-minute limit.
2. Merge #1265 first: `gh pr merge 1265 --merge`.
3. #1267 will probably conflict with #1265 in
   `docs/implementation/12-current-app-guide.md`, because both add a section.
   Merge `origin/develop` into the #1267 worktree, keep both sections, commit,
   push, and wait for CI again. Then `gh pr merge 1267 --merge`. #1267 says
   "Part of #1264", so #1264 stays open.
4. Then follow `02-local-deploy-runbook.md`.

## What each PR does

**#1265: screenshot cap**
- Each store has its own cap, enforced by its own writer:
  - the daemon archive, by the daemon;
  - `<screenshot_folder>/.seraph-daemon-captures`, by the backend.
- The daemon archives a capture only when the backend didn't confirm the
  upload, so captures are no longer stored twice.
- Pruning deletes the oldest files first, by age and then by size, bounded per
  run.
- It deletes only when limits are actually known:
  - Daemon: limits count only if both arrive as integers; otherwise it keeps
    its last received limits.
  - Backend: an unreadable settings file means no prune.
- The settings file is written atomically.
- Missing images degrade to 200 with `image_available: false`.

**#1267: GitHub activity source, slice 1**
- An opt-in source that reads through the operator's local `gh` login:
  - GET only, through allow-listed endpoints, with no shell.
  - Never `gh auth token`; a minimal environment; a pinned binary path.
- Three new tables, keyed by `owner_identity_id`, and a Settings panel.
- Effective-grants rows, and an ADR-011 addendum.
- `run_owned_argv` is extracted from `run_command`.
- It is owned by the operator identity, not the login session:
  - No time-based expiry.
  - It stops on identity revoke, workspace restore or rollback, local-root
    retirement, toggle-off, grant revoke, or a gh login change.
