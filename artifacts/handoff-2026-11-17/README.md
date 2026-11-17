# Team-lead handoff: 2026-11-17

This is the state of the Seraph team-lead session on 2026-11-17, written as the
usage limit approached. Read this file first, then the topic files below.

| File | What it covers |
|---|---|
| [01-open-prs-and-merge-steps.md](01-open-prs-and-merge-steps.md) | The two approved PRs waiting on CI, and exactly how to land them |
| [02-local-deploy-runbook.md](02-local-deploy-runbook.md) | How to update and verify the local Seraph on the Mac (daemon, gh source) |
| [03-next-work-1264-slice2.md](03-next-work-1264-slice2.md) | The next feature: GitHub activity in the end-of-day goal report |
| [04-followups-and-decisions.md](04-followups-and-decisions.md) | Open follow-up issues, known pre-existing failures, and operator decisions |
| [design-1263.md](design-1263.md) | Fable 5.2 design for the screenshot cap (#1263) |
| [design-1264.md](design-1264.md) | Fable 5.2 design (revision 2, gh CLI) for GitHub activity in reports (#1264) |

## How the work is run

- **Team lead:** a lead session hands each issue to a worker subagent in
  `.agent-worktrees/<slug>`. Then an independent read-only critic reviews the
  PR. The operator asked for **Fable 5.2 at high effort** as designer and
  reviewer for the current features.
- **Merging:** the lead merges a PR once it is reviewed and CI is fully green,
  with a plain `gh pr merge N --merge`. No rebase or force-push; to update a
  branch, merge develop into it.
- **Operator rules that every brief carries:**
  - No time-based guards or TTL gates. Validity is keyed to real changes only.
  - No sleeps or retries to hide races. Keep every behavioural assertion.
  - Never read or copy `.env.dev`. Never print secrets.
  - Never start the capture daemon from an agent shell. macOS would then ask
    to grant Claude, not Seraph, Screen Recording. The operator starts it from
    iTerm2. `manage.sh -e dev local up` auto-starts the daemon when
    `DAEMON_ENABLED=true`, so run `./manage.sh -e dev daemon stop` right after
    any agent-run restart.
  - Never self-approve Seraph in-app approvals; the operator approves in the UI.
  - `artifacts/eval-2026-11-09/` (93 MB) is intentionally uncommitted.

## Merged on 2026-11-17

| PR | Fixes |
|---|---|
| #1249 | Last two leak exceptions (#1235, #1247) |
| #1250 | Daemon `/api/observer/context` posts authenticated (#1248) |
| #1251 | Bounded pipe capture shutdown (#1234) |
| #1252 | Recovery deadline flake (#1230) |
| #1253 | Review follow-ups (#1227) |
| #1254 | `get_session` patches 119 → 4 (#1241, partial) |
| #1260 | `run_command` cleanup when capture setup fails (#1255) |
| #1261 | Daemon notification receipts authenticated (#1258) |
| #1262 | `session_scope` cancel rollback, checker dynamic imports, `_ACTIVE_WORKER_TASKS` registry (#1256, #1257, #1259) |
| #1268 | Lifecycle transition claim race; develop CI was red because of it |

Verified live on the Mac at develop `e2c40f10`:
- Window switches: daemon context posts get 200.
- Screenshots: captures get 201.
- Notifications: a test notification's display-attempted and ack posts each get 200.
