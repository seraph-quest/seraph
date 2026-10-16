# Backend container seccomp profile (#1103)

- `docker-default.json` is the base: Docker's default seccomp profile
  (moby `profiles/seccomp/default.json`) as transcribed for this change.
  The transcription was made offline, and syscalls newer than moby v28.0.0
  were left out, so the base can only deny more than Docker, never less.
  The `container-process-sandbox` CI job fetches moby's `v28.0.0` profile and
  fails if any base rule is missing from it
  (`scripts/check_container_process_sandbox.py --compare-seccomp`).
- `seraph-backend.json` is the profile Compose loads. It is exactly
  `docker-default.json` plus the rules whose `comment` starts with
  `seraph #1103`: `clone` and `unshare` with user, mount, pid, net, ipc and
  uts namespace flags (cgroup and time namespaces stay denied), plus `mount`,
  `umount2` and `pivot_root`. `backend/tests/test_container_process_sandbox.py`
  enforces that composition offline.

To refresh the base, replace `docker-default.json` with the upstream file,
rebuild `seraph-backend.json` as the base plus the `seraph #1103` rules, and
run the container tests.
