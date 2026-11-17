# Design: enforce `archive_max_mb` / `archive_retention_days` on stored screenshots (#1263)

Base: `develop` at `e2c40f10`. Read-only design; nothing was edited, committed or posted.
Paths are relative to `/Users/bigcube/Desktop/repos/seraph`.

## 0. What the code does today (verified)

| Fact | Evidence |
| --- | --- |
| The daemon uploads and archives the **same bytes**. `uploader.upload(client, png_bytes, ...)` base64-encodes `png_bytes` unmodified; `_archive_provider_capture(png_bytes=png_bytes)` writes the same buffer. | `daemon/capture_upload.py:248`, `daemon/seraph_daemon.py:1048-1075` and `:1326-1353` |
| When the backend stored the upload, the daemon drops the JSON observation, so the archive paths (`capture_artifacts`) never reach the backend. | `_context_observation_payload`, `daemon/seraph_daemon.py:745-754` |
| Even when they are posted (unpaired / failed upload), the backend API ignores them: `_screen_capture_artifacts` returns `None` for any provider other than `screenshot_folder` / `paired_edge`, and the daemon's provider is always `apple-vision`. | `backend/src/api/observer.py:868-885`, test `test_screen_artifacts_ignore_named_external_provider` |
| So the daemon archive is, for current daemons, a write-only local store. Only legacy rows (artifacts with no `provider`) are still served from it. | same |
| The upload store is `<screenshot_folder>/.seraph-daemon-captures/<YYYY-MM-DD>/<capture_id>.{png,jpg}`; observations reference it by absolute `image_path` in the `capture_artifacts:` detail (provider `screenshot_folder`). The analysis job, `/image`, `/analysis` and `/reanalyze` read that file. | `backend/src/observer/local_capture_upload.py`, `screenshot_folder_source.py:545-570` |
| The upload happens whether or not `preserve_captures` is on; the archive only when it is on and local analysis succeeded. | `daemon/seraph_daemon.py:1047-1075` |
| Uploads are loopback-only, so when both stores are written they are on the same Mac. The archive can still be on a host/filesystem the backend cannot see (container backend, unpaired remote daemon). | `validate_local_backend_origin`, `daemon/capture_upload.py:47-60` |
| The backend's default archive root is `~/Library/Application Support/Seraph/artifacts/screen-captures`, independent of `WORKSPACE_DIR`, including under pytest. | `screen_analysis_settings.py:260-267`, `api/observer.py:815-832`, `api/settings.py:422-432` |
| On a failed settings fetch the daemon falls back to CLI values, which have no budget keys, so `archive_max_mb` becomes 0 and retention 365. | `daemon/seraph_daemon.py:493, 511-512` |
| Operator machine (sizes and counts only): archive 406 MB = 448 PNG + 896 JSON sidecars; upload store 242 MB = 269 PNG; the whole screenshot folder is the upload store. About 0.9 MB per PNG, so 10 GiB is roughly 11,000 captures. | `du`/`find` counts |
| No frontend code renders a screenshot image or exposes `archive_max_mb`. The panel shows counts and the inspection endpoint as text. | `frontend/src/components/settings/ArtifactStoragePanel.tsx` |

## 1. Decisions

### 1.1 Ownership: the writer of a store enforces it

- **Daemon** owns `<archive_dir>`: size cap and `archive_retention_days`.
- **Backend** owns `<screenshot_folder>/.seraph-daemon-captures`: size cap and age retention.
- **The backend never deletes in the daemon archive.** Two reasons: it may not see that filesystem, and its default archive root is a real home-directory path even in tests, so a backend-side archive prune would let `run_screen_cleanup()` in an ordinary test delete an operator's files.
- Each owner is the only writer of its store, so write and prune are serialised in-process with no cross-process lock.

### 1.2 Cap scope: per store, with one copy per capture

The operator's "10 GB" means "Seraph must not keep more than about 10 GB of screenshots on this disk". Delivered as:

- `archive_max_mb` is a cap on **each** Seraph-owned store (MiB, `0` = no cap).
- A capture is stored **once** (see 1.3): paired and uploading, only the upload store grows; otherwise only the archive grows. In either steady state the total is bounded by the cap.
- Stated limit: after a topology switch, or a long run of failed uploads (the archive is the fallback), the non-growing store can hold up to another cap until its retention ages it out. Worst case 2 x cap. This is the one place the plan differs from the issue text ("archive plus upload store ≤ cap"); see section 6 for the stricter alternative and why it is not in this PR.

### 1.3 Dedupe: stop writing the archive copy when the backend stored the upload

Yes, they are duplicates, and the archive copy is the one nothing reads. Change: the daemon archives only when the backend does **not** hold the capture (`upload_result is None or not upload_result.stored`): unpaired, upload failed, blocked, over 8 MiB, cool-down. This keeps "the daemon keeps capturing locally" for those cases and halves growth when paired (269 kept files instead of 269 + 448).

Consequence to state in docs: a near-duplicate frame the backend suppressed (`stored` is true, backend kept no file) is now kept nowhere. That is the backend's existing visual-run dedupe policy.

The existing 406 MB in `artifacts/screen-captures` is not touched by this change beyond the configured cap and retention. Deleting it is an operator decision.

### 1.4 Limits source and safe direction

- Backend: `archive_max_mb` and `archive_retention_days` from `read_screen_analysis_settings()`. Upload-store age retention becomes `min(settings.screen_observation_retention_days, archive_retention_days)`: the upload store is the PNG archive when paired, so the operator's archive retention must bound it. Defaults (90 vs 365) leave today's behaviour unchanged.
- Daemon: deletes only under limits it **received from the backend in this process**. Before the first successful settings fetch it deletes nothing. After a failed fetch it keeps the last received limits instead of dropping to 0 / 365 (AGENTS.md: preserve last-known settings through metadata failures).
- Every failure path deletes less, never more: unreadable settings, unsafe root, scan bound, I/O error.

### 1.5 Measuring size: a bounded walk, no incremental state

- Stateless `os.scandir`/`lstat` walk of the owner's store, run only when `max_bytes > 0`. At 10 GiB that is about 11k files (backend) or about 33k with sidecars (daemon): tens of milliseconds, next to an upload path that already hashes the image, fingerprints it with PIL and runs several `LIKE` scans.
- No module-level counter: nothing to register in `process_state_registry`, nothing to drift when files are removed by hand.
- Bounds per run: `max_scan = 200_000` entries, `max_files = 5_000` deletions. Hitting either is reported as `over_cap=True` in the result and the audit event, not hidden. Day directories are scanned oldest first so a truncated scan still sees the oldest files.

## 2. Pruning algorithm (both stores)

Inputs: `root`, `max_bytes`, `retention_days` (or `None`), `now`, bounds. `now` is a parameter; the functions read no clock.

1. **Root check.** Backend: existing `_require_private_store(upload_root)` (not a symlink, a directory, owned by this uid); otherwise return `skipped_reason="capture_store_unsafe"` with nothing deleted. Daemon: resolve `archive_dir` as the writer does (an operator symlink to another disk is legitimate), then require a directory owned by this uid and not `/` or the home directory.
2. **Scan.** Never follow symlinks (`follow_symlinks=False` everywhere, symlinked directories not entered). Only regular files (`S_ISREG`) are candidates.
   - Backend candidates: `.png` / `.jpg` (prunable), `.partial` (counted, age-only). Same suffix rule as today's cleanup.
   - Daemon candidates: only names Seraph writes, in a `YYYY-MM-DD` directory directly under the root:
     `^(?P<stem>\d{6}-[a-z0-9_-]{1,40}-[0-9a-f]{16})\.(png|analysis\.json|[a-z0-9_-]{1,40}\.(json|txt))$` (`.codex.txt` is the legacy sidecar). A capture is the group of files sharing `(day, stem)`; a group is deleted whole so no sidecar is orphaned. Anything else in the directory is neither counted nor deleted.
3. **Order.** Oldest first by `(st_mtime_ns, relative path)`; the path tie-break makes equal mtimes deterministic. A daemon group's mtime is the minimum of its members.
4. **Age pass** (when `retention_days` is given): delete entries with `mtime < now - retention_days`.
5. **Size pass** (when `max_bytes > 0`): `total` = bytes of surviving candidates; delete oldest prunable entries until `total <= max_bytes`. `.partial` files are never size-pruned (an in-flight write).
6. **Delete.** Re-`lstat` immediately before `unlink` and require a regular file. `FileNotFoundError` counts as freed. Any other `OSError` leaves the bytes in `total` and continues.
7. Remove day directories that became empty (`rmdir`, errors ignored).
8. Return a result dataclass: deleted counts (expired / for size), freed bytes, remaining bytes and files, `over_cap`, `skipped_reason`.

Operator screenshot-folder images are safe by construction: the backend walk is rooted at `.seraph-daemon-captures` only, and `_image_paths` already excludes that subtree from folder scans. A path-based `lstat` walk is sufficient because the store is `0700` and owned by the process user; anyone able to swap a directory inside it can already delete the operator's files directly.

### When it runs

| Store | Trigger | Passes |
| --- | --- | --- |
| Upload (backend) | After each **accepted** upload, inside `screenshot_ingest_lock()`, after the observation row exists. Skipped entirely when the cap is 0. | size |
| Upload (backend) | `screen_cleanup` job (03:00 daily, exempt retention job), now also inside `screenshot_ingest_lock()`. | age + size |
| Archive (daemon) | After each archive write. | size (skipped when cap is 0) |
| Archive (daemon) | When `(archive_dir, retention_days, archive_max_mb)` received from the backend changes, including the first successful fetch after start. | age + size |
| Archive (daemon) | UTC day rollover (the existing `_refresh_daily_counter` event). The daemon has no cleanup job; this is its equivalent. | age + size |

### Concurrency

- Backend writers already hold `screenshot_ingest_lock()`. The after-write prune runs under the same lock in `asyncio.to_thread`; the cleanup job takes the lock too, so no prune overlaps a write, a `.partial` rename or a capture-id hash check.
- Readers outside the lock (`/image`, the analysis job) tolerate a vanished file: `FileResponse` on an unlinked open file is fine; the analysis job already catches `OSError` and marks `source_missing` on the next pass.
- Daemon: archive write and prune are synchronous on the event loop, so the two capture loops cannot interleave with a prune. No thread, no lock.
- No sleeps, retries or timers are added.

## 3. Records that reference a deleted PNG

Reference field: the `capture_artifacts:{...}` entry in `ScreenObservation.details_json` (`image_path`, `relative_path`, `image_sha256`, `image_bytes`, `width`, `height`, `file_format`; legacy rows also `provider_output_path` / `codex_output_path` / `analysis_path`). Digests (`MemoryEpisode`) and report receipts store no image path; the end-of-day report uses only the file **name** as a label.

| Reader | With the file gone today | Required |
| --- | --- | --- |
| `GET /api/observer/screen-artifacts` (`_screen_artifact_response`) | Lists the row; no file access. | Add `artifacts.image_available: bool`. |
| `GET .../{id}/image` | 404 `Screen artifact file not found`. | Keep. 404 is "unavailable"; it is not a 500. |
| `GET .../{id}/analysis`, `screenshot_folder` rows | 404 from `_artifact_path`, although the analysis lives in the DB; `local_image_metadata` would raise if the file vanished between check and stat. | **Change:** 200 with the stored analysis, `image_available: false`, metadata from the stored artifacts, and a note that retention removed the image. |
| `GET .../{id}/analysis`, legacy rows with `analysis_path` | 404 when the sidecar is gone. | **Change:** 200 `{provider, summary, activity, project, analysis: null, analysis_available: false}` from the observation row. |
| `GET .../{id}/provider-output` | 404. | Keep. |
| `POST .../{id}/reanalyze` | 404 before any mutation. | Keep; add a test that `details_json` is unchanged. |
| Analysis job (`analyze_pending_screenshot_folder_observations`) | `pending` / `failed` rows become `source_missing`, reason `image file not found`, no provider call. `succeeded` and `blocked` rows are never reselected, so a stored analysis is never stripped. | None. |
| Digest / end-of-day report | Read no files. They exclude rows whose status is `source_missing` unless analysis succeeded. | None; pin with tests. |
| Settings summary and "Clear stale" | `source_missing_count` rises; "Clear stale" hides those rows (`blocked=True`). | None. |
| Upload idempotency | Same `capture_id` again returns `duplicate`; the file is not rewritten. Same bytes under a new id are suppressed as `exact_hash_duplicate`. | None; pin with a test. |
| Cockpit | Renders no images. | Only the new capture-storage row (section 4.4). |

Visible consequence to document: a capture pruned **before** its governed analysis ran becomes `source_missing` and leaves digests and reports. Pruning and the analysis job both go oldest first, so this needs a cap smaller than the analysis backlog.

## 4. Implementation plan

Branch `fix/1263-capture-store-limits` from `develop`. One aggregate PR. Suggested commit slices: 4.1, 4.2, 4.3, 4.4, 4.5.

### 4.1 Backend: prune the upload store

`backend/src/observer/local_capture_upload.py`

- Add `logger`, `from config.settings import settings`, `MIB = 1024 * 1024`.
- Add frozen dataclass `CaptureStorePruneResult(deleted_files=0, deleted_expired=0, deleted_for_size=0, freed_bytes=0, remaining_bytes=0, remaining_files=0, over_cap=False, skipped_reason=None)`.
- Add `prune_local_capture_uploads(*, retention_days: int | None, max_bytes: int = 0, screenshot_root: Path | None = None, now: datetime | None = None, max_files: int = 5_000, max_scan: int = 200_000) -> CaptureStorePruneResult` implementing section 2.
- Keep `cleanup_local_capture_uploads(...) -> int` with its current signature as a thin wrapper returning `.deleted_files`. Both existing tests of it must pass unmodified.
- Add `capture_store_limits() -> tuple[int, int]`: `(archive_max_mb * MIB, min(max(1, settings.screen_observation_retention_days), archive_retention_days))`, importing `read_screen_analysis_settings` inside the function.
- Add `async def _enforce_store_cap(screenshot_root) -> CaptureStorePruneResult`: returns an empty result without walking when the cap is 0; otherwise `await asyncio.to_thread(prune_local_capture_uploads, retention_days=None, max_bytes=..., screenshot_root=...)`. An exception is logged and returned as `skipped_reason="prune_failed"`; it never fails an accepted capture.
- `LocalCaptureResult`: add `pruned_files: int = 0`, `pruned_bytes: int = 0`, `prune_skipped_reason: str | None = None`.
- `ingest_local_capture`: on the `accepted` path only, after `screen_observation_repo.create(...)` succeeds and still inside the lock, call `_enforce_store_cap(screenshot_root)` and copy its numbers into the result.

`backend/src/api/observer.py` (`upload_local_capture`)

- Add `pruned_files` / `pruned_bytes` to the existing audit details when non-zero. When `prune_skipped_reason` is set, log a second `log_integration_event(integration_type="observer_daemon", name="capture_store_prune", outcome="failed", details={"reason": ...})`. The HTTP response schema is unchanged.

`backend/src/scheduler/jobs/screen_cleanup.py`

- `_cleanup_capture_uploads()` returns `CaptureStorePruneResult`: read `capture_store_limits()`, then `async with screenshot_ingest_lock(): await asyncio.to_thread(prune_local_capture_uploads, retention_days=..., max_bytes=...)`. Keep the "never fails the job" `try/except`, returning an empty result.
- `run_screen_cleanup`: `deleted_capture_files = result.deleted_files`. The skip branch and its reason `no_expired_observations` are unchanged. Add to the `succeeded` details: `deleted_capture_files_for_size`, `capture_store_bytes`, `capture_cap_mb`, `capture_retention_days`, `capture_prune_over_cap`, `capture_prune_skipped_reason`.

### 4.2 Backend: missing-file readers

`backend/src/api/observer.py`

- Split `_artifact_path` into `_resolve_artifact_path(raw, *, allowed_roots) -> Path` (404 for an empty path, 403 outside the roots, no existence check) plus the existing existence check, and add `_artifact_path_if_present(...) -> Path | None`. The 403 must still be decided before existence, so `test_screen_artifact_endpoints_reject_paths_outside_archive` holds.
- `_screen_artifact_response`: `image_available` is `True` for `paired_edge`, otherwise whether the resolved path is a file. Never raises.
- `get_screen_artifact_analysis`: the two degraded 200 responses from section 3.
- `_screenshot_folder_image_analysis(image_path: Path | None, ...)`: use `local_image_metadata` when the file is present (guard `OSError`), else `image_bytes` / `file_format` / `width` / `height` from the stored artifacts; add `image_available`; keep every existing key.

### 4.3 Daemon: dedupe and prune the archive

New `daemon/capture_archive.py` (pure, importable on any OS, no PyObjC)

- `MIB`, `DAY_DIR_RE`, `ARCHIVE_FILE_RE`, `MAX_SCAN_ENTRIES`, `MAX_DELETE_CAPTURES`.
- `ArchivePruneResult(status, reason, scanned_files, deleted_captures, deleted_files, freed_bytes, remaining_bytes, over_cap)`.
- `prune_capture_archive(archive_dir: str, *, max_bytes: int, retention_days: int, now: datetime, max_scan=..., max_delete=...) -> ArchivePruneResult` per section 2. Never raises.

`daemon/seraph_daemon.py`

- Import `prune_capture_archive`, `MIB` with the same try/except-ImportError pattern as `capture_upload`.
- `ScreenAnalysisRuntime.__init__`: new optional `now: Callable[[], datetime] | None`; store `self._now`; use it in `__init__` and `_refresh_daily_counter` in place of `datetime.now(timezone.utc)`. New fields `_archive_limits_known = False`, `_archive_policy = None`, `_last_archive_prune = None`.
- `refresh()`: keep `fetched = await fetch_screen_analysis_settings(...)`; `payload = fetched or self._fallback`. When `fetched` is truthy, read the two archive limits from it and set `_archive_limits_known = True`. When it is not, reuse `self.archive_retention_days` / `self.archive_max_mb`. Everything else, including the signature tuple, is unchanged.
- `_update_budget_settings(...)`: after assigning, if `_archive_limits_known` and `(self.archive_dir, retention_days, archive_max_mb) != self._archive_policy`, store the tuple and call `self.prune_archive(reason="settings")`. All four call sites already set `self.archive_dir` first.
- `_refresh_daily_counter()`: on a date change also call `self.prune_archive(reason="day_rollover")`.
- New `prune_archive(self, *, reason: str) -> None`: return when limits are unknown; return when `reason == "write"` and `archive_max_mb == 0`; otherwise call `prune_capture_archive(...)` with `now=self._now()`, store a small dict in `_last_archive_prune`, log at info when anything was deleted. Catches and logs every exception.
- `budget_status()`: add `"archive_prune": self._last_archive_prune` (flows to `daemon-status.json`).
- New module function `_archive_capture(screen_runtime, *, png_bytes, app_name, analysis, upload) -> dict | None`: returns `None` when `preserve_captures` is off or `upload is not None and upload.stored`; otherwise calls `_archive_provider_capture(...)`, then `getattr(screen_runtime, "prune_archive", None)` if callable with `reason="write"`. Replace both call sites (`:1068-1075`, `:1346-1353`), keeping the `"capture_artifacts" not in observation` guard and assigning only a non-`None` result. Test doubles built from `SimpleNamespace` have no `prune_archive` and keep working.
- `_archive_provider_capture` is unchanged (tests import it).

### 4.4 Operator-visible state

- `backend/src/api/settings.py`: `_screenshot_folder_summary` already walks the folder; accumulate `capture_store_bytes` / `capture_store_file_count` for paths under `LOCAL_CAPTURE_UPLOAD_DIRNAME` in that same loop (and zeros / `None` in the early-return and fallback shapes). In `get_artifact_storage_settings` add `screenshot_folder.capture_store = {"bytes", "file_count", "max_mb", "retention_days", "cap_scope": "per_store", "enforced_by": "backend"}`. This file is a clock-seam module: add no direct clock read.
- `frontend/src/components/settings/ArtifactStoragePanel.tsx`: extend the `screenshot_folder` type with optional `capture_store`; add one `ArtifactRow` "Stored captures" in the screenshot-folder section: `242 MB of 10240 MB cap · kept 90 days`, or `242 MB · no size cap · kept 90 days`, or `usage unavailable` when `bytes` is null. Read-only; no new control.
- This slice can be cut without affecting enforcement; the docs in 4.5 cannot.

### 4.5 Docs

- `docs/implementation/12-current-app-guide.md`, "Daemon capture handoff" paragraph: one copy per capture; per-store cap and retention; who enforces which store; when pruning runs; what a missing image looks like (`image_available`, `source_missing`); the daemon deletes only under limits received from the backend.
- `daemon/README.md`: `--preserve-captures` / `--capture-archive-dir` rows and the privacy paragraph (archive is the fallback store when the backend did not store the upload; limits come from backend settings).
- `env.dev.example`, `env.prod.example`: comment above `SCREEN_CAPTURE_ARCHIVE_*` saying both are enforced, per store.
- `docs/implementation/STATUS.md` only if it carries a row for capture retention.

### Data flow after the change

```
capture -> local OCR -> upload to loopback backend
   stored (accepted/duplicate/suppressed)            not stored (unpaired, failed, blocked, >8 MiB)
   |                                                 |
   backend: write file + observation (ingest lock)   daemon: _archive_provider_capture (if preserve)
   backend: prune upload store to cap (same lock)    daemon: prune archive to cap
   daemon: no archive copy                           context post carries capture_artifacts

03:00 screen_cleanup: observations by age; upload store by age + cap (ingest lock)
daemon: settings change / first fetch / UTC day rollover -> archive by age + cap
```

## 5. Test plan

No sleeps, no retries, no wall-clock assertions. File times are set with `os.utime` to fixed values and `now` is passed in. Settings and env go through `monkeypatch.setattr` / `monkeypatch.setenv` (the determinism ratchet counts direct assignment). DB tests use the `db` / `async_db` fixture; `get_session` is never patched. No baseline change is expected in `scripts/test_determinism_baseline.json`.

### Backend, new `backend/tests/test_capture_store_prune.py` (sync, `tmp_path` only)

1. Cap removes oldest until `total <= cap`: five 400-byte files with increasing mtimes, cap 1000, leaves the newest two; assert counts, `freed_bytes == 1200`, `remaining_bytes == 800`, `over_cap is False`.
2. Cap 0 with `retention_days=None` removes nothing.
3. Operator images in the screenshot root and in a sibling directory are neither deleted nor counted (a 400-byte store under a 500-byte cap survives next to 10 KB of operator PNGs).
4. Symlinks: a symlinked day directory, and a symlink file inside a real day directory pointing at an operator image, are not followed, not counted and not removed; the targets keep their bytes.
5. A symlinked store root returns `skipped_reason == "capture_store_unsafe"` with a cap set.
6. Equal mtimes are deleted in path order.
7. A fresh `.partial` is not size-pruned; an expired one is removed by the age pass.
8. Age then size in one run: expired files go first and do not count toward the total.
9. `max_files=2` with five files over the cap deletes two and reports `over_cap is True`; a second run finishes and reports `False`.
10. A file that vanishes before `unlink` (monkeypatched `os.unlink` that removes the file then raises `FileNotFoundError` once) is counted as freed and the run completes.

### Backend, `backend/tests/test_observer_local_capture_upload.py` (existing `capture_env`)

11. Upload past the cap: `monkeypatch.setattr(settings, "screen_capture_archive_max_mb", 1)`; three distinct incompressible PNGs of about 0.45 MiB (seeded `random.Random(n).randbytes`, so they are not visual duplicates). Before each next upload set the stored files' mtimes to fixed older values with `os.utime`. After the third: the first file is gone, the other two remain, all three observations exist; the list shows `image_available` false / true / true; `/image` for the first is 404; `/analysis` for the first is 200 with `image_available: false` and the stored width and height.
12. Re-uploading the pruned `capture_id` returns 200 `duplicate` and does not recreate the file.
13. Cap 0: `prune_local_capture_uploads` monkeypatched to raise `AssertionError`; the upload is still 201.
14. Prune raising `OSError`: the upload is still 201 and the observation exists.
15. Digest and report with a missing image: after an upload carrying `local_analysis`, unlink the stored file; `digest._screenshot_observations` and `_screen_summary_for_local_day` still return the record. Then run `analyze_pending_screenshot_folder_observations` with `analyze_screenshot_image` patched to raise if called: status becomes `source_missing` and the row leaves the digest. This pins the documented behaviour.
16. `test_cleanup_removes_only_expired_uploads` and `test_symlinked_capture_store_is_refused_and_not_cleaned` pass unmodified.

### Backend, job and API

17. `backend/tests/test_scheduler_job_audit.py`: `run_screen_cleanup()` with a tmp screenshot folder holding three 600 KB files and a 1 MiB cap deletes the oldest and records `scheduler_job_succeeded` with `deleted_capture_files == 1` and the new size fields. `test_screen_cleanup_logs_skip_when_nothing_deleted` passes unmodified.
18. Same file: a persisted `archive_retention_days` of 30 removes a file dated 40 days back while one dated 20 days back stays (day-scale margins, as the existing cleanup test does).
19. `backend/tests/test_observer_screen_artifacts.py`: folder image deleted after scan gives `image_available: false`, `/image` 404 with the existing detail, `/analysis` 200 degraded, `/reanalyze` 404 with `details_json` byte-identical before and after.
20. Same file: a legacy archive row whose files are removed gives `/analysis` 200 with `analysis_available: false` and the observation summary; `/provider-output` 404.
21. `backend/tests/test_settings_api.py`: `/api/settings/artifact-storage` returns `screenshot_folder.capture_store` with the bytes and count of files under `.seraph-daemon-captures`, the configured `max_mb` and the effective `retention_days`.

### Frontend

22. `frontend/src/components/settings/ArtifactStoragePanel.test.tsx`: the row renders for a capped store, an uncapped store and a payload without `capture_store`.

### Daemon (`cd backend && uv run pytest ../daemon/tests`)

New `daemon/tests/test_capture_archive.py` (no Darwin skip; pure):

- a. Cap removes the oldest capture **groups** (PNG and both sidecars together) until `<= cap`; cap 0 removes nothing.
- b. Retention removes groups older than N days relative to the injected `now`; a group exactly at the cutoff stays.
- c. Files Seraph did not name (`notes.txt`, `Screenshot 2026-11-16.png`, anything in a non-date directory, files directly in the root) are neither counted nor deleted.
- d. A symlinked day directory and a symlinked file are untouched.
- e. Missing root returns `status == "skipped"`; a root equal to a monkeypatched `Path.home()` is refused and nothing is deleted.
- f. `max_delete` bound reports `over_cap`.
- g. A legacy `.codex.txt` sidecar is grouped with its PNG.

In `daemon/tests/test_periodic_capture.py` (Darwin-gated, existing helpers):

- h. Every file `_archive_provider_capture` writes matches `ARCHIVE_FILE_RE` and shares one stem (drift guard between writer and pruner).
- i. With an uploader fake returning a stored result and `preserve_captures=True`, the archive directory stays empty and the context post carries no observation.
- j. With an uploader fake returning `failed`, the capture is archived and an older group is pruned under a small cap.
- k. `ScreenAnalysisRuntime` with a settings client returning non-200: files two years older than the injected `now` survive `refresh()` (limits unknown). After a 200 payload with `archive_retention_days: 180` they are removed. After a further failed fetch `archive_max_mb` and `archive_retention_days` keep the received values.
- l. Advancing the injected `now` by a day and calling `budget_status()` prunes a group that crossed retention.
- m. `test_detailed_mode_posts_preserved_capture_artifacts` and the other existing daemon tests pass unmodified.

### Validation commands for the worker

```
cd backend && uv run pytest tests/test_capture_store_prune.py tests/test_observer_local_capture_upload.py \
  tests/test_observer_screen_artifacts.py tests/test_scheduler_job_audit.py tests/test_settings_api.py \
  tests/test_screenshot_observation_digest.py tests/test_end_of_day_goal_report.py tests/test_process_state_registry.py
cd backend && uv run pytest ../daemon/tests        # one known pre-existing failure: test_spool_bounds_dedupe_and_retry_backoff_are_durable
cd backend && uv run python scripts/determinism_lanes.py stress --files tests/test_capture_store_prune.py
cd frontend && npm test -- src/components/settings/ArtifactStoragePanel.test.tsx --maxWorkers=1 && npm run build
python3 scripts/check_test_determinism.py && python3 scripts/check_docs_contract.py && python3 scripts/check_strategy_claims.py
git diff --check
```

Runtime proof after merge (operator machine, daemon restarted): authenticated `GET /api/settings/artifact-storage` shows `capture_store`; `daemon-status.json` shows `capture_budget.archive_prune`; `artifacts/screen-captures` stops growing while uploads are stored.

## 6. Risks and changes to the issue

### Risks

1. **Per-store, not combined.** Worst case 2 x cap after a topology switch or weeks of failed uploads. A strict combined cap is possible without cross-process deletes: the upload response returns the store's byte total (already known from the prune walk) and the daemon sizes its archive budget as `cap - upload_bytes`. It adds a protocol field, daemon state and an "archive yields to upload store" rule under which fallback captures are dropped once the upload store is full. Recommended as a follow-up issue only if the operator wants the hard total.
2. **New deletions on upgrade.** The daemon now enforces `archive_retention_days` (default 365) on existing archive files the first time it receives settings. Say so in the PR and docs.
3. **Existing 406 MB archive** on the operator's machine stays until retention; it is unreferenced duplicate data. Ask the operator whether to delete it.
4. **Captures pruned before analysis** become `source_missing` and leave digests and reports (section 3). Visible as the "missing" count.
5. **A cap smaller than one capture** (up to 8 MiB) deletes each new file immediately. `PUT /settings/screen-analysis` accepts any value `>= 0`; this plan does not change validation.
6. **The daemon must be restarted** to pick up the change; an old daemon keeps writing both copies with no cap.
7. **`mtime` ordering.** A restore tool that resets mtimes makes ordering fall back to path order and restarts age retention (already true of the upload store).
8. **Scan bound.** Above 200k files the cap may not be met in one run; reported as `over_cap`.
9. **Pruned files are gone.** Private captures are not backed up (#1141).
10. **Daemon blocks its loop** for the walk (about 33k `lstat` at a full 10 GiB archive), only on archive writes, which are rare once paired.
11. **Pre-existing, not fixed here:** `preserve_captures=false` does not stop the backend upload store from keeping PNGs; a store left behind after the operator changes `screenshot_folder` or `archive_dir` is never pruned; the backend resolves the archive root in three separate functions. Each deserves its own issue.

### Changes I would make to issue #1263

- Replace "the daemon archive plus the private upload store are kept at or under that many MiB" with: each Seraph-owned store is kept at or under the cap by the process that writes it, and a capture is stored once.
- Add the dedupe to intended behaviour and acceptance (no archive copy when the backend stored the upload).
- Replace "runs after each write and on the existing cleanup job" for the daemon with its real triggers: after each archive write, on a change of received limits, and on UTC day rollover.
- Add: the daemon deletes only under limits received from the backend and keeps the last received limits through an outage.
- Add: `archive_retention_days` also bounds the upload store (the shorter of it and observation retention).
- Make "unavailable, not an error" concrete: `image_available` in the list, `/analysis` 200 degraded, `/image` 404, pending rows become `source_missing`.
- Add: the backend never prunes the daemon archive root.
