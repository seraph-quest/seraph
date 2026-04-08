"""Run one backend test shard as isolated per-file pytest subprocesses."""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

try:
    from scripts.backend_test_shards import shard_for_index
except ModuleNotFoundError:  # pragma: no cover - CI script entrypoint fallback
    from backend_test_shards import shard_for_index


RUNTIME_HEAVY_FILE_TIMEOUTS: dict[str, int] = {
    "tests/test_approvals_api.py": 1_200,
    "tests/test_context_window.py": 1_200,
    "tests/test_delivery.py": 1_200,
    "tests/test_eval_harness.py": 1_500,
    "tests/test_observer_api.py": 1_200,
    "tests/test_tools_api.py": 1_500,
    "tests/test_workflows.py": 1_500,
}

SPECIALIZED_TEST_INVOCATIONS: dict[str, list[tuple[str, list[str]]]] = {
    "tests/test_capabilities_api.py": [
        (
            "tests/test_capabilities_api.py::overview_and_catalog",
            [
                "tests/test_capabilities_api.py",
                "-k",
                "load_starter_packs or attach_ or mcp_status or doctor_reports or capabilities_overview",
            ],
        ),
        (
            "tests/test_capabilities_api.py::starter_pack_activation_foundations",
            [
                "tests/test_capabilities_api.py",
                "-k",
                "activate_starter_pack_enables_seeded_assets or activate_manifest_backed_starter_pack_works or ensure_bundled_workflow_available",
            ],
        ),
        (
            "tests/test_capabilities_api.py::starter_pack_activation_bundled_core",
            [
                "tests/test_capabilities_api.py",
                "-k",
                "activate_bundled_core_capability_pack_uses_manifest_runtime or activate_bundled_core_capability_pack_uses_real_catalog_install",
            ],
        ),
        (
            "tests/test_capabilities_api.py::starter_pack_activation_approvals_and_degraded",
            [
                "tests/test_capabilities_api.py",
                "-k",
                "activate_starter_pack_requires_catalog_install_approval or activate_starter_pack_preflights_all_approvals_without_consuming_them or activate_starter_pack_reports_degraded_when_enable_fails",
            ],
        ),
        (
            "tests/test_capabilities_api.py::bootstrap_manual_routes",
            [
                "tests/test_capabilities_api.py",
                "-k",
                "capability_bootstrap_leaves_policy_changes_manual or capability_bootstrap_leaves_mcp_enable_actions_manual or capability_bootstrap_leaves_extension_enable_actions_manual",
            ],
        ),
        (
            "tests/test_capabilities_api.py::bootstrap_apply_and_validation",
            [
                "tests/test_capabilities_api.py",
                "-k",
                "capability_preflight_returns_workflow_and_runbook_repair_metadata or capability_bootstrap_can_apply_low_risk_toggle_actions or capability_bootstrap_does_not_reclassify_low_risk_actions_as_manual_after_failed_apply or workflow_draft_validation_and_save",
            ],
        ),
    ],
    "tests/test_delivery.py": [
        (
            "tests/test_delivery.py::channel_and_bundle",
            [
                "tests/test_delivery.py",
                "-k",
                "native_channel or channel_routing or queued_bundle",
            ],
        ),
        (
            "tests/test_delivery.py::remaining",
            [
                "tests/test_delivery.py",
                "-k",
                "not (native_channel or channel_routing or queued_bundle)",
            ],
        ),
    ],
    "tests/test_eval_harness.py": [
        (
            "tests/test_eval_harness.py::runtime_group_1",
            [
                "tests/test_eval_harness.py",
                "-k",
                "test_run_runtime_evals_passes_group_1 and not source_report_action_workflow_behavior",
            ],
        ),
        (
            "tests/test_eval_harness.py::test_source_report_action_workflow_behavior_runtime_eval_details",
            [
                "tests/test_eval_harness.py",
                "-k",
                "test_source_report_action_workflow_behavior_runtime_eval_details",
            ],
        ),
        (
            "tests/test_eval_harness.py::runtime_group_2",
            [
                "tests/test_eval_harness.py",
                "-k",
                "test_run_runtime_evals_passes_group_2",
            ],
        ),
        (
            "tests/test_eval_harness.py::runtime_group_3",
            [
                "tests/test_eval_harness.py",
                "-k",
                "test_run_runtime_evals_passes_group_3",
            ],
        ),
        (
            "tests/test_eval_harness.py::runtime_group_4",
            [
                "tests/test_eval_harness.py",
                "-k",
                "test_run_runtime_evals_passes_group_4",
            ],
        ),
        (
            "tests/test_eval_harness.py::remaining",
            [
                "tests/test_eval_harness.py",
                "-k",
                "not (test_run_runtime_evals_passes_group_1 or test_source_report_action_workflow_behavior_runtime_eval_details or test_run_runtime_evals_passes_group_2 or test_run_runtime_evals_passes_group_3 or test_run_runtime_evals_passes_group_4)",
            ],
        ),
    ],
    "tests/test_observer_api.py": [
        (
            "tests/test_observer_api.py::continuity_and_notifications",
            [
                "tests/test_observer_api.py",
                "-k",
                "continuity or native_notification or intervention_feedback",
            ],
        ),
        (
            "tests/test_observer_api.py::remaining",
            [
                "tests/test_observer_api.py",
                "-k",
                "not (continuity or native_notification or intervention_feedback)",
            ],
        ),
    ],
    "tests/test_tools_api.py": [
        (
            "tests/test_tools_api.py::native_policy_modes",
            [
                "tests/test_tools_api.py",
                "-k",
                "full_mode_includes_execute_code or safe_mode_keeps_clarify_available or safe_mode_keeps_todo_available or safe_mode_keeps_session_search_available or safe_mode_keeps_browser_session_available or safe_mode_keeps_get_scheduled_jobs_available or balanced_mode_hides_full_only_tools",
            ],
        ),
        (
            "tests/test_tools_api.py::delegation_and_scheduler",
            [
                "tests/test_tools_api.py",
                "-k",
                "balanced_mode_keeps_delegate_task_available or balanced_mode_keeps_manage_scheduled_job_available or hides_delegate_task_when_delegation_is_disabled",
            ],
        ),
        (
            "tests/test_tools_api.py::mcp_policy_surface",
            [
                "tests/test_tools_api.py",
                "-k",
                "hides_mcp_tools_when_disabled or marks_mcp_tools_as_approval_required_in_approval_mode or marks_authenticated_mcp_tools_with_narrower_boundary or allows_mcp_tools_with_balanced_native_policy_when_mcp_approval_enabled",
            ],
        ),
        (
            "tests/test_tools_api.py::workflow_boundary_surface",
            [
                "tests/test_tools_api.py",
                "-k",
                "surfaces_workflow_execution_boundaries",
            ],
        ),
    ],
    "tests/test_workflows.py": [
        (
            "tests/test_workflows.py::approval_and_legacy_boundary_drift",
            [
                "tests/test_workflows.py",
                "-k",
                "approval_context or legacy_checkpoint",
            ],
        ),
        (
            "tests/test_workflows.py::authenticated_source_boundary_drift",
            [
                "tests/test_workflows.py",
                "-k",
                "authenticated_source",
            ],
        ),
        (
            "tests/test_workflows.py::delegation_boundary_drift",
            [
                "tests/test_workflows.py",
                "-k",
                "delegated_specialist or delegated_tool_inventory",
            ],
        ),
        (
            "tests/test_workflows.py::history_and_projection_surface",
            [
                "tests/test_workflows.py",
                "-k",
                "projects_history or stored_fingerprint or pending_run_lacks_tracked_authenticated_context or marks_waiting_runs_as_awaiting_approval or does_not_suggest_tool_policy or hides_later_retry_draft or disambiguates_duplicate_fingerprinted_runs",
            ],
        ),
        (
            "tests/test_workflows.py::resume_plan_branching_surface",
            [
                "tests/test_workflows.py",
                "-k",
                "returns_structured_branch_metadata or rejects_approval_gate or rejects_noninitial_checkpoint or blocks_branching_past_pending_approval_gate or falls_back_to_scoped_run_lookup",
            ],
        ),
        (
            "tests/test_workflows.py::remaining_boundary_surface",
            [
                "tests/test_workflows.py",
                "-k",
                "not (approval_context or authenticated_source or delegated_specialist or delegated_tool_inventory or legacy_checkpoint or projects_history or stored_fingerprint or pending_run_lacks_tracked_authenticated_context or marks_waiting_runs_as_awaiting_approval or does_not_suggest_tool_policy or hides_later_retry_draft or disambiguates_duplicate_fingerprinted_runs or returns_structured_branch_metadata or rejects_approval_gate or rejects_noninitial_checkpoint or blocks_branching_past_pending_approval_gate or falls_back_to_scoped_run_lookup)",
            ],
        ),
    ],
}


def timeout_for_file(path: str, default_timeout_seconds: int | None) -> int | None:
    hinted_timeout = RUNTIME_HEAVY_FILE_TIMEOUTS.get(path)
    if default_timeout_seconds is None:
        return hinted_timeout
    if hinted_timeout is None:
        return default_timeout_seconds
    return max(default_timeout_seconds, hinted_timeout)


def pytest_invocations_for_target(path: str) -> list[tuple[str, list[str]]]:
    return SPECIALIZED_TEST_INVOCATIONS.get(path, [(path, [path])])


def run_shard_files(
    root: Path,
    files: list[str],
    *,
    pytest_args: list[str] | None = None,
    file_timeout_seconds: int | None = None,
) -> int:
    if not files:
        print("No backend tests assigned to this shard.")
        return 0

    extra_args = list(pytest_args or [])
    for path in files:
        for label, invocation_args in pytest_invocations_for_target(path):
            command = [sys.executable, "-m", "pytest", "-q", *invocation_args, *extra_args]
            timeout_seconds = timeout_for_file(path, file_timeout_seconds)
            started_at = time.perf_counter()
            try:
                completed = subprocess.run(
                    command,
                    cwd=root,
                    check=False,
                    timeout=timeout_seconds,
                )
            except subprocess.TimeoutExpired:
                duration_s = time.perf_counter() - started_at
                print(
                    f"[backend-shard] {label} -> 124 ({duration_s:.2f}s) timed out after "
                    f"{timeout_seconds}s"
                )
                return 124
            duration_s = time.perf_counter() - started_at
            print(
                f"[backend-shard] {label} -> {completed.returncode} ({duration_s:.2f}s)"
                f"{f' timeout={timeout_seconds}s' if timeout_seconds is not None else ''}"
            )
            if completed.returncode != 0:
                return completed.returncode
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--shard-count", type=int, required=True)
    parser.add_argument("--shard-index", type=int, required=True)
    parser.add_argument("--file-timeout-seconds", type=int, default=None)
    parser.add_argument("pytest_args", nargs=argparse.REMAINDER)
    args = parser.parse_args()

    root = Path(__file__).resolve().parents[1]
    files = shard_for_index(root, shard_count=args.shard_count, shard_index=args.shard_index)
    extra_args = list(args.pytest_args)
    if extra_args[:1] == ["--"]:
        extra_args = extra_args[1:]
    return run_shard_files(
        root,
        files,
        pytest_args=extra_args,
        file_timeout_seconds=args.file_timeout_seconds,
    )


if __name__ == "__main__":
    raise SystemExit(main())
