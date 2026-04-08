import sys
from pathlib import Path
from subprocess import CompletedProcess, TimeoutExpired
from unittest.mock import patch

from scripts.run_backend_test_shard import (
    pytest_invocations_for_target,
    run_shard_files,
    timeout_for_file,
)


def test_run_shard_files_executes_each_file_in_isolation(tmp_path: Path):
    files = ["tests/test_alpha.py", "tests/test_beta.py"]

    with patch("scripts.run_backend_test_shard.subprocess.run") as mock_run:
        mock_run.side_effect = [
            CompletedProcess(args=["pytest"], returncode=0),
            CompletedProcess(args=["pytest"], returncode=0),
        ]

        result = run_shard_files(tmp_path, files, pytest_args=["-x"])

    assert result == 0
    first_call = mock_run.call_args_list[0]
    second_call = mock_run.call_args_list[1]
    assert first_call.kwargs["cwd"] == tmp_path
    assert first_call.args[0][0] == sys.executable
    assert first_call.args[0][1:4] == ["-m", "pytest", "-q"]
    assert "tests/test_alpha.py" in first_call.args[0]
    assert "-x" in first_call.args[0]
    assert "tests/test_beta.py" in second_call.args[0]
    assert "-x" in second_call.args[0]


def test_run_shard_files_supports_script_dir_import_fallback(tmp_path: Path):
    files = ["tests/test_alpha.py"]

    with patch("scripts.run_backend_test_shard.subprocess.run") as mock_run:
        mock_run.return_value = CompletedProcess(args=["pytest"], returncode=0)

        result = run_shard_files(tmp_path, files)

    assert result == 0
    assert mock_run.call_args.args[0][0] == sys.executable


def test_run_shard_files_stops_after_first_failure(tmp_path: Path):
    files = ["tests/test_alpha.py", "tests/test_beta.py"]

    with patch("scripts.run_backend_test_shard.subprocess.run") as mock_run:
        mock_run.side_effect = [
            CompletedProcess(args=["pytest"], returncode=1),
            CompletedProcess(args=["pytest"], returncode=0),
        ]

        result = run_shard_files(tmp_path, files)

    assert result == 1
    assert mock_run.call_count == 1


def test_run_shard_files_accepts_empty_shards(tmp_path: Path):
    assert run_shard_files(tmp_path, []) == 0


def test_run_shard_files_passes_per_file_timeout(tmp_path: Path):
    files = ["tests/test_alpha.py"]

    with patch("scripts.run_backend_test_shard.subprocess.run") as mock_run:
        mock_run.return_value = CompletedProcess(args=["pytest"], returncode=0)

        result = run_shard_files(tmp_path, files, file_timeout_seconds=900)

    assert result == 0
    assert mock_run.call_args.kwargs["timeout"] == 900


def test_timeout_for_file_uses_runtime_heavy_override():
    assert timeout_for_file("tests/test_workflows.py", 900) == 1_500
    assert timeout_for_file("tests/test_eval_harness.py", None) == 1_500
    assert timeout_for_file("tests/test_approvals_api.py", 900) == 1_200
    assert timeout_for_file("tests/test_context_window.py", 900) == 1_200
    assert timeout_for_file("tests/test_delivery.py", 900) == 1_200
    assert timeout_for_file("tests/test_tools_api.py", 900) == 1_500
    assert timeout_for_file("tests/test_alpha.py", 900) == 900


def test_pytest_invocations_for_target_splits_eval_harness_contract():
    invocations = pytest_invocations_for_target("tests/test_eval_harness.py")

    assert invocations == [
        (
            "tests/test_eval_harness.py::runtime_group_1",
            [
                "tests/test_eval_harness.py",
                "-k",
                "test_run_runtime_evals_passes_group_1 and not source_report_action_workflow_behavior",
            ],
        ),
        (
            "tests/test_eval_harness.py::source_report_action_workflow_behavior",
            [
                "tests/test_eval_harness.py",
                "-k",
                "source_report_action_workflow_behavior",
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
                "not (test_run_runtime_evals_passes_group_1 or test_run_runtime_evals_passes_group_2 or test_run_runtime_evals_passes_group_3 or test_run_runtime_evals_passes_group_4)",
            ],
        ),
    ]


def test_run_shard_files_returns_timeout_code_when_file_hangs(tmp_path: Path):
    files = ["tests/test_alpha.py", "tests/test_beta.py"]

    with patch("scripts.run_backend_test_shard.subprocess.run") as mock_run:
        mock_run.side_effect = TimeoutExpired(cmd=["pytest"], timeout=600)

        result = run_shard_files(tmp_path, files, file_timeout_seconds=600)

    assert result == 124
    assert mock_run.call_count == 1


def test_run_shard_files_uses_heavy_file_timeout_override(tmp_path: Path):
    files = ["tests/test_workflows.py"]

    with patch("scripts.run_backend_test_shard.subprocess.run") as mock_run:
        mock_run.return_value = CompletedProcess(args=["pytest"], returncode=0)

        result = run_shard_files(tmp_path, files, file_timeout_seconds=900)

    assert result == 0
    assert mock_run.call_args.kwargs["timeout"] == 1_500


def test_run_shard_files_executes_specialized_eval_targets_in_order(tmp_path: Path):
    files = ["tests/test_eval_harness.py"]

    with patch("scripts.run_backend_test_shard.subprocess.run") as mock_run:
        mock_run.side_effect = [
            CompletedProcess(args=["pytest"], returncode=0),
            CompletedProcess(args=["pytest"], returncode=0),
            CompletedProcess(args=["pytest"], returncode=0),
            CompletedProcess(args=["pytest"], returncode=0),
            CompletedProcess(args=["pytest"], returncode=0),
            CompletedProcess(args=["pytest"], returncode=0),
        ]

        result = run_shard_files(tmp_path, files, file_timeout_seconds=900)

    assert result == 0
    assert mock_run.call_count == 6
    first_command = mock_run.call_args_list[0].args[0]
    second_command = mock_run.call_args_list[1].args[0]
    third_command = mock_run.call_args_list[2].args[0]
    fourth_command = mock_run.call_args_list[3].args[0]
    fifth_command = mock_run.call_args_list[4].args[0]
    sixth_command = mock_run.call_args_list[5].args[0]
    assert first_command[4:7] == [
        "tests/test_eval_harness.py",
        "-k",
        "test_run_runtime_evals_passes_group_1 and not source_report_action_workflow_behavior",
    ]
    assert second_command[4:7] == [
        "tests/test_eval_harness.py",
        "-k",
        "source_report_action_workflow_behavior",
    ]
    assert third_command[4:7] == [
        "tests/test_eval_harness.py",
        "-k",
        "test_run_runtime_evals_passes_group_2",
    ]
    assert fourth_command[4:7] == [
        "tests/test_eval_harness.py",
        "-k",
        "test_run_runtime_evals_passes_group_3",
    ]
    assert fifth_command[4:7] == [
        "tests/test_eval_harness.py",
        "-k",
        "test_run_runtime_evals_passes_group_4",
    ]
    assert sixth_command[4:7] == [
        "tests/test_eval_harness.py",
        "-k",
        "not (test_run_runtime_evals_passes_group_1 or test_run_runtime_evals_passes_group_2 or test_run_runtime_evals_passes_group_3 or test_run_runtime_evals_passes_group_4)",
    ]
    assert mock_run.call_args_list[0].kwargs["timeout"] == 1_500


def test_pytest_invocations_for_target_splits_workflows_contract():
    invocations = pytest_invocations_for_target("tests/test_workflows.py")

    assert invocations == [
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
    ]


def test_pytest_invocations_for_target_splits_delivery_contract():
    invocations = pytest_invocations_for_target("tests/test_delivery.py")

    assert invocations == [
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
    ]


def test_pytest_invocations_for_target_splits_capabilities_api_contract():
    invocations = pytest_invocations_for_target("tests/test_capabilities_api.py")

    assert invocations == [
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
    ]


def test_pytest_invocations_for_target_splits_observer_api_contract():
    invocations = pytest_invocations_for_target("tests/test_observer_api.py")

    assert invocations == [
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
    ]
