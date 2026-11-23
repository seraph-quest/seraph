#!/usr/bin/env bash
# Called by a "Classify failures" step when the backend test environment
# step failed, so no test ran (#1290). Reports the failure as infrastructure
# when the environment action recorded a network/runner-image cause, and as
# an unclassified setup failure otherwise. Never as a test failure. Always
# exits 1: the job stays red.
set -uo pipefail

infra_file="${RUNNER_TEMP:?}/seraph-ci-infrastructure.txt"
if [ -s "$infra_file" ]; then
  reason="$(tr '\n' ' ' < "$infra_file")"
  line="[determinism] infrastructure failure before any test ran: ${reason}- not a test failure; no test node to classify"
  echo "::error title=CI infrastructure (not a test failure)::${reason}"
else
  line="[determinism] setup failure before any test ran (backend test environment step failed; see its log) - not a test failure; no test node to classify"
  echo "::error title=CI setup failure (not a test failure)::backend test environment step failed before any test ran"
fi
echo "$line"
echo "$line" >> "${GITHUB_STEP_SUMMARY:-/dev/null}"
exit 1
