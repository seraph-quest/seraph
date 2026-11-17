# Local deploy runbook (operator's Mac)

## Current local state

- Seraph runs from the main checkout at develop `e2c40f10`, through
  `./manage.sh -e dev local`.
  - Backend: `127.0.0.1:8004`, log `logs/seraph-local-backend.log`.
  - Frontend: `127.0.0.1:3001`.
- Workspace: `~/.seraph/dev-data` (`LOCAL_WORKSPACE_DIR` in `.env.dev`).
  `USER_TIMEZONE=Europe/Warsaw`. Auth: local no-login.
- Daemon:
  - Paired as `mac-screen-daemon`; the credential is in
    `~/.seraph/edge-credential` (mode 0600).
  - Log: `daemon/seraph-daemon.log`.
  - Status: `~/.seraph/dev-data/daemon-status.json`.
- Screenshot settings: `archive_max_mb=10240` and `archive_retention_days=365`.
  The operator chose "keep PNGs, 10 GB cap". Nothing enforces the cap until
  #1265 is deployed.
- Six goals were imported from the old docker-data database. The end-of-day
  report runs at 21:00 local and is read through `GET /api/reports/end-of-day`.
- Mutating API calls need the header `Origin: http://127.0.0.1:3001`.

## Deploying after #1265 and #1267 merge

1. Bring the checkout up to date and restart the stack:
   ```bash
   cd ~/Desktop/repos/seraph
   git switch develop
   git pull --ff-only
   export PATH="$HOME/.local/seraph-npm-11.8.0/bin:$HOME/.asdf/installs/nodejs/lts/bin:$PATH"
   ./manage.sh -e dev local down
   ./manage.sh -e dev local up
   ./manage.sh -e dev daemon stop
   ```
   When an agent runs this, the last command is mandatory, because `local up`
   auto-starts the daemon.
2. The operator starts the daemon **from iTerm2**:
   `./manage.sh -e dev daemon start`.
3. Verify the daemon:
   - `logs/seraph-local-backend.log` shows `POST /api/observer/context 200`
     and `POST /api/observer/captures 201`.
   - After #1265, `daemon-status.json` has `capture_budget.archive_prune`.
   - `/api/settings/artifact-storage` shows `screenshot_folder.capture_store`.
4. Optional pre-check, done on 2026-11-17: all 1,830 archive files matched
   `ARCHIVE_FILE_RE`, and the oldest capture was from 2026-11-16. Nothing is
   deleted under 10 GB / 365 days.
5. Turn on the GitHub source. The operator does this in the UI:
   1. Open Settings → GitHub → "GitHub activity for reports".
   2. Press **Check gh login**. It should show `gh <version> at
      /opt/homebrew/bin/gh, logged in as @<login>`. This is the first real gh
      run, and nobody has verified yet that gh can reach the keychain from the
      backend's minimal environment.
   3. Optionally put `seraph-quest/seraph` in "Always read".
   4. Tick the consent box. Tick "Include … in the end-of-day report's model
      analysis" only if wanted.
   5. Press **Enable**, then **Collect now**.

   This works only for a host-run backend, not Docker.

## Known local issues

- The Docker VM disk is almost full (91.2 of 91.4 GB). The operator should
  prune unused images and build cache. Agents must not delete it.
- The old 406 MB daemon archive holds duplicate data that nothing reads. It is
  not removed automatically; deleting it is the operator's call.
