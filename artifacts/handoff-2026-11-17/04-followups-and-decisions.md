# Follow-ups, known failures, decisions

## Open follow-up issues filed today

| Issue | Topic |
|---|---|
| #1264 | GitHub activity in the report; slice 1 is PR #1267, slice 2 is next |
| #1266 | A save while the screenshot settings file is unreadable writes default limits back; plus small #1265 review notes |
| #1269 | Lifecycle transition claim hardening: a task-identity check instead of a flag, honest status when reactivation fails, and a test variant that doesn't need Cordis |
| #1270 | GitHub source: pin restore and identity wiring, and the lost-identity gap (do before slice 2) |
| #1241 | Still open for items 1, 2 and 4 (DB seam gaps) |

## Pre-existing test failures (fail on clean develop; not filed yet)

- `daemon/tests/test_paired_edge_transport.py::test_spool_bounds_dedupe_and_retry_backoff_are_durable`
  fails with `assert 0 == 1` on the spool count.
- `backend/tests/test_repo_sandbox_settings_loader_rejects_corruption_and_symlink`
- `backend/tests/test_document_pairs.py::test_authenticated_private_pair_reserve_stream_seal_and_exact_bind[*]`
  fails in 6 cases on this Mac, with `document_resource_self_check_failed`.
- `backend/tests/test_process_tools.py` can't run on macOS (no `/proc`,
  `RLIMIT_AS`, BSD sed). Prove it on Linux CI or in the Docker runner.

## Operator decisions (2026-11-17)

- **Screenshots:** keep PNGs with a 10 GB cap, enforced separately for each
  store. A hard combined limit is a possible later follow-up.
- **GitHub credential path:** the local `gh` CLI login. Fable had recommended a
  pasted read-only token.
- **gh binary pin:** lenient. The hash is recorded on each run without
  blocking, so `brew upgrade gh` doesn't switch the source off.
- **GitHub source ownership:** the source belongs to the operator identity, not
  to a login session. It has no time-based expiry, and an explicit logout
  doesn't stop it.
- **Designer and reviewer:** Fable 5.2 high for the current features. When a
  worker is stuck, it escalates to Fable.

## Not tracked yet (raise if wanted)

- The macOS process-identity gap: there is no `/proc`. It is a security design
  question.
- Six DBG prints in `api/evolution.py`.
- A3.3 slices #1205–#1211, and the NEAR vision and embeddings slots, which need
  UI actions from the operator.
