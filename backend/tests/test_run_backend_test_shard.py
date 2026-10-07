import ast
import re
import sys
from pathlib import Path
from subprocess import CompletedProcess, TimeoutExpired
from unittest.mock import patch

from scripts.run_backend_test_shard import (
    CANCELLED_EVAL_CONSUMER_NODES,
    FULLY_RETIRED_EVAL_INVOCATIONS,
    main,
    pytest_invocations_for_target,
    run_shard_files,
    timeout_for_file,
)


def test_ci_retirement_skips_exact_module_before_invocation(tmp_path, capsys):
    with patch("scripts.run_backend_test_shard.subprocess.run") as run, patch(
        "scripts.run_backend_test_shard.pytest_invocations_for_target",
        return_value=[("ordinary", ["tests/test_alpha.py"])],
    ) as invocations:
        run.return_value = CompletedProcess(args=["pytest"], returncode=0)
        result = run_shard_files(tmp_path, ["tests/test_eval_harness.py", "tests/test_alpha.py"],
                                 exclude_cancelled_eval_harness=True)
    assert result == 0
    invocations.assert_called_once_with("tests/test_alpha.py")
    run.assert_called_once()
    assert "tests/test_eval_harness.py" not in run.call_args.args[0]
    output = capsys.readouterr().out
    assert "RETIRED/NOT RUN tests/test_eval_harness.py" in output
    assert "228 top-level test definitions, including 13 ordinary contracts" in output
    assert "not selected for execution; not passed (source-definition counts)" in output


def test_ci_retirement_does_not_skip_similar_module_names(tmp_path):
    files = ["tests/test_eval_harness_extra.py", "tests/subdir/test_eval_harness.py"]
    with patch("scripts.run_backend_test_shard.subprocess.run") as run:
        run.return_value = CompletedProcess(args=["pytest"], returncode=0)
        assert run_shard_files(tmp_path, files, exclude_cancelled_eval_harness=True) == 0
    assert [call.args[0][4] for call in run.call_args_list] == files


def test_ci_retirement_only_shard_reports_unrun_without_subprocess(tmp_path, capsys):
    with patch("scripts.run_backend_test_shard.subprocess.run") as run:
        assert run_shard_files(tmp_path, ["tests/test_eval_harness.py"],
                               exclude_cancelled_eval_harness=True) == 0
    run.assert_not_called()
    assert "RETIRED/NOT RUN" in capsys.readouterr().out


def test_ci_retirement_preserves_ordinary_failure_propagation(tmp_path):
    with patch("scripts.run_backend_test_shard.subprocess.run") as run:
        run.return_value = CompletedProcess(args=["pytest"], returncode=1)
        result = run_shard_files(tmp_path, ["tests/test_eval_harness.py", "tests/test_alpha.py",
                                           "tests/test_beta.py"], exclude_cancelled_eval_harness=True)
    assert result == 1
    run.assert_called_once()
    assert "tests/test_alpha.py" in run.call_args.args[0]


def test_ci_retirement_preserves_ordinary_timeout_propagation(tmp_path):
    with patch("scripts.run_backend_test_shard.subprocess.run") as run:
        run.side_effect = TimeoutExpired(cmd=["pytest"], timeout=900)
        result = run_shard_files(tmp_path, ["tests/test_eval_harness.py", "tests/test_alpha.py",
                                           "tests/test_beta.py"], file_timeout_seconds=900,
                                 exclude_cancelled_eval_harness=True)
    assert result == 124
    run.assert_called_once()
    assert run.call_args.kwargs["timeout"] == 900


def test_ci_retirement_cli_forwards_fixed_policy_and_preserves_arguments():
    with patch.object(sys, "argv", ["runner", "--shard-count", "10", "--shard-index", "2",
                                   "--file-timeout-seconds", "900", "--exclude-cancelled-eval-harness",
                                   "--", "-x"]), patch(
        "scripts.run_backend_test_shard.shard_for_index", return_value=["tests/test_alpha.py"],
    ) as shard, patch("scripts.run_backend_test_shard.run_shard_files", return_value=1) as run:
        assert main() == 1
    assert shard.call_args.kwargs == {"shard_count": 10, "shard_index": 2}
    assert run.call_args.args[1] == ["tests/test_alpha.py"]
    assert run.call_args.kwargs == {"pytest_args": ["-x"], "file_timeout_seconds": 900,
                                    "exclude_cancelled_eval_harness": True}


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


def _expected_eval_harness_invocations():
    runtime_group_1_filter = (
        "test_run_runtime_evals_passes_group_1 "
        "and not source_report_action_workflow_behavior "
        "and not chat_model_wrapper_runtime_eval_details "
        "and not rest_chat_behavior_runtime_eval_details "
        "and not rest_chat_approval_contract_runtime_eval_details "
        "and not rest_chat_timeout_contract_runtime_eval_details "
        "and not websocket_chat_behavior_runtime_eval_details "
        "and not websocket_chat_approval_contract_runtime_eval_details "
        "and not websocket_chat_timeout_contract_runtime_eval_details"
    )
    remaining_filter = (
        "not (test_run_runtime_evals_passes_group_1 "
        "or test_chat_model_wrapper_runtime_eval_details "
        "or test_rest_chat_behavior_runtime_eval_details "
        "or test_rest_chat_approval_contract_runtime_eval_details "
        "or test_rest_chat_timeout_contract_runtime_eval_details "
        "or test_websocket_chat_behavior_runtime_eval_details "
        "or test_websocket_chat_approval_contract_runtime_eval_details "
        "or test_websocket_chat_timeout_contract_runtime_eval_details "
        "or test_source_report_action_workflow_behavior_runtime_eval_details "
        "or test_runtime_eval_scenarios_expose_expected_details "
        "or test_run_runtime_evals_passes_group_2 "
        "or test_run_runtime_evals_passes_group_3 "
        "or test_run_runtime_evals_passes_group_4)"
    )
    return [
        (
            "tests/test_eval_harness.py::runtime_group_1",
            [
                "tests/test_eval_harness.py",
                "-k",
                runtime_group_1_filter,
            ],
        ),
        (
            "tests/test_eval_harness.py::test_chat_model_wrapper_runtime_eval_details",
            [
                "tests/test_eval_harness.py",
                "-k",
                "test_chat_model_wrapper_runtime_eval_details",
            ],
        ),
        (
            "tests/test_eval_harness.py::test_rest_chat_behavior_runtime_eval_details",
            [
                "tests/test_eval_harness.py",
                "-k",
                "test_rest_chat_behavior_runtime_eval_details",
            ],
        ),
        (
            "tests/test_eval_harness.py::test_rest_chat_approval_contract_runtime_eval_details",
            [
                "tests/test_eval_harness.py",
                "-k",
                "test_rest_chat_approval_contract_runtime_eval_details",
            ],
        ),
        (
            "tests/test_eval_harness.py::test_rest_chat_timeout_contract_runtime_eval_details",
            [
                "tests/test_eval_harness.py",
                "-k",
                "test_rest_chat_timeout_contract_runtime_eval_details",
            ],
        ),
        (
            "tests/test_eval_harness.py::test_websocket_chat_behavior_runtime_eval_details",
            [
                "tests/test_eval_harness.py",
                "-k",
                "test_websocket_chat_behavior_runtime_eval_details",
            ],
        ),
        (
            "tests/test_eval_harness.py::test_websocket_chat_approval_contract_runtime_eval_details",
            [
                "tests/test_eval_harness.py",
                "-k",
                "test_websocket_chat_approval_contract_runtime_eval_details",
            ],
        ),
        (
            "tests/test_eval_harness.py::test_websocket_chat_timeout_contract_runtime_eval_details",
            [
                "tests/test_eval_harness.py",
                "-k",
                "test_websocket_chat_timeout_contract_runtime_eval_details",
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
            "tests/test_eval_harness.py::test_runtime_eval_scenarios_expose_expected_details",
            [
                "tests/test_eval_harness.py",
                "-k",
                "test_runtime_eval_scenarios_expose_expected_details",
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
                remaining_filter,
            ],
        ),
    ]


def test_pytest_invocations_for_target_splits_eval_harness_contract():
    invocations = pytest_invocations_for_target("tests/test_eval_harness.py")

    assert invocations == _expected_eval_harness_invocations()


def test_pytest_invocations_for_target_splits_e2e_conversation_contract():
    invocations = pytest_invocations_for_target("tests/test_e2e_conversation.py")

    assert [label for label, _ in invocations] == [
        "tests/test_e2e_conversation.py::test_full_message_flow",
        "tests/test_e2e_conversation.py::test_seq_numbers_monotonically_increase",
        "tests/test_e2e_conversation.py::test_tool_name_in_step_content",
        "tests/test_e2e_conversation.py::test_agent_run_success_is_written_to_audit_log",
        "tests/test_e2e_conversation.py::test_high_risk_tool_sends_approval_required_message",
        "tests/test_e2e_conversation.py::test_missing_input_sends_clarification_required_message",
        "tests/test_e2e_conversation.py::test_timeout_logs_only_timed_out_runtime_event",
        "tests/test_e2e_conversation.py::test_secret_values_are_redacted_in_streamed_messages",
        "tests/test_e2e_conversation.py::test_resume_message_does_not_duplicate_user_turn",
    ]
    assert invocations[4][1] == [
        "tests/test_e2e_conversation.py::TestE2EConversation::test_high_risk_tool_sends_approval_required_message",
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
    expected_invocations = _expected_eval_harness_invocations()

    with patch("scripts.run_backend_test_shard.subprocess.run") as mock_run:
        mock_run.side_effect = [CompletedProcess(args=["pytest"], returncode=0)] * len(expected_invocations)

        result = run_shard_files(tmp_path, files, file_timeout_seconds=900)

    assert result == 0
    assert mock_run.call_count == len(expected_invocations)
    actual_filters = [call.args[0][4:7] for call in mock_run.call_args_list]
    expected_filters = [invocation for _, invocation in expected_invocations]
    assert actual_filters == expected_filters
    assert mock_run.call_args_list[0].kwargs["timeout"] == 1_500


def test_run_shard_files_executes_specialized_tools_targets_in_order(tmp_path: Path):
    files = ["tests/test_tools_api.py"]

    with patch("scripts.run_backend_test_shard.subprocess.run") as mock_run:
        mock_run.side_effect = [
            CompletedProcess(args=["pytest"], returncode=0),
            CompletedProcess(args=["pytest"], returncode=0),
            CompletedProcess(args=["pytest"], returncode=0),
            CompletedProcess(args=["pytest"], returncode=0),
        ]

        result = run_shard_files(tmp_path, files, file_timeout_seconds=900)

    assert result == 0
    assert mock_run.call_count == 4
    first_command = mock_run.call_args_list[0].args[0]
    second_command = mock_run.call_args_list[1].args[0]
    third_command = mock_run.call_args_list[2].args[0]
    fourth_command = mock_run.call_args_list[3].args[0]
    assert first_command[4:7] == [
        "tests/test_tools_api.py",
        "-k",
        "full_mode_includes_execute_code or safe_mode_keeps_clarify_available or safe_mode_keeps_todo_available or safe_mode_keeps_session_search_available or safe_mode_keeps_browser_session_available or safe_mode_keeps_get_scheduled_jobs_available or balanced_mode_hides_full_only_tools",
    ]
    assert second_command[4:7] == [
        "tests/test_tools_api.py",
        "-k",
        "balanced_mode_keeps_delegate_task_available or balanced_mode_keeps_manage_scheduled_job_available or hides_delegate_task_when_delegation_is_disabled",
    ]
    assert third_command[4:7] == [
        "tests/test_tools_api.py",
        "-k",
        "hides_mcp_tools_when_disabled or marks_mcp_tools_as_approval_required_in_approval_mode or marks_authenticated_mcp_tools_with_narrower_boundary or allows_mcp_tools_with_balanced_native_policy_when_mcp_approval_enabled",
    ]
    assert fourth_command[4:7] == [
        "tests/test_tools_api.py",
        "-k",
        "surfaces_workflow_execution_boundaries",
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


def test_pytest_invocations_for_target_splits_tools_api_contract():
    invocations = pytest_invocations_for_target("tests/test_tools_api.py")

    assert invocations == [
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


def test_ci_consumer_retirement_is_fixed_exact_source_inventory():
    assert len(CANCELLED_EVAL_CONSUMER_NODES) == 37
    assert sum(map(len, CANCELLED_EVAL_CONSUMER_NODES.values())) == 109
    assert "tests/test_operator_api.py::test_operator_computer_use_benchmark_surface_reports_policy_and_receipts" in CANCELLED_EVAL_CONSUMER_NODES["tests/test_operator_api.py"]
    assert "tests/test_continuous_orchestration_slo.py::test_continuous_orchestration_slo_report_runs_batch_cs_suites" in CANCELLED_EVAL_CONSUMER_NODES["tests/test_continuous_orchestration_slo.py"]
    assert "tests/test_memory_providers.py" not in CANCELLED_EVAL_CONSUMER_NODES
    assert "tests/test_memory_benchmark.py" not in CANCELLED_EVAL_CONSUMER_NODES
    assert "tests/test_post_dx_reach_voice_media_parity.py" not in CANCELLED_EVAL_CONSUMER_NODES
    assert "tests/test_operator_api.py::test_operator_computer_use_benchmark_surface_degrades_summary_on_failures" not in CANCELLED_EVAL_CONSUMER_NODES["tests/test_operator_api.py"]


def test_ci_consumer_retirement_appends_exact_nodes_without_changing_original_groups(tmp_path, capsys):
    path = "tests/test_operator_api.py"
    original = [(label, args) for label, args in pytest_invocations_for_target(path)
                if (path, label) not in FULLY_RETIRED_EVAL_INVOCATIONS]
    with patch("scripts.run_backend_test_shard.subprocess.run") as run:
        run.return_value = CompletedProcess(args=["pytest"], returncode=0)
        assert run_shard_files(tmp_path, [path], pytest_args=["-x"], exclude_cancelled_eval_harness=True) == 0
    expected_retired = [f"--deselect={node}" for node in CANCELLED_EVAL_CONSUMER_NODES[path]]
    assert len(run.call_args_list) == len(original)
    for call, (_, args) in zip(run.call_args_list, original):
        assert call.args[0] == [sys.executable, "-m", "pytest", "-q", *args, "-x", "--no-cov", *expected_retired]
    output = capsys.readouterr().out
    for node in CANCELLED_EVAL_CONSUMER_NODES[path]:
        assert f"RETIRED/NOT RUN {node}:" in output


def test_ci_consumer_retirement_does_not_apply_to_similar_or_unmapped_modules(tmp_path):
    files = ["tests/test_operator_api_extra.py", "tests/test_memory_providers.py", "tests/test_memory_benchmark.py"]
    with patch("scripts.run_backend_test_shard.subprocess.run") as run:
        run.return_value = CompletedProcess(args=["pytest"], returncode=0)
        assert run_shard_files(tmp_path, files, exclude_cancelled_eval_harness=True) == 0
    assert len(run.call_args_list) == 3
    assert all(not arg.startswith("--deselect=") for call in run.call_args_list for arg in call.args[0])


def test_ci_consumer_retirement_disabled_preserves_original_invocation(tmp_path):
    path = "tests/test_continuous_orchestration_slo.py"
    with patch("scripts.run_backend_test_shard.subprocess.run") as run:
        run.return_value = CompletedProcess(args=["pytest"], returncode=0)
        assert run_shard_files(tmp_path, [path]) == 0
    assert run.call_args.args[0] == [sys.executable, "-m", "pytest", "-q", path, "--no-cov"]


def test_ci_consumer_retirement_keeps_mapped_failure_failfast(tmp_path):
    with patch("scripts.run_backend_test_shard.subprocess.run") as run:
        run.return_value = CompletedProcess(args=["pytest"], returncode=1)
        assert run_shard_files(tmp_path, ["tests/test_operator_api.py", "tests/test_alpha.py"], exclude_cancelled_eval_harness=True) == 1
    run.assert_called_once()
    assert any(arg.startswith("--deselect=tests/test_operator_api.py::") for arg in run.call_args.args[0])


def test_ci_consumer_retirement_keeps_mapped_timeout_failfast(tmp_path):
    with patch("scripts.run_backend_test_shard.subprocess.run") as run:
        run.side_effect = TimeoutExpired(cmd=["pytest"], timeout=900)
        assert run_shard_files(tmp_path, ["tests/test_operator_api.py", "tests/test_alpha.py"], file_timeout_seconds=900, exclude_cancelled_eval_harness=True) == 124
    run.assert_called_once()
    assert run.call_args.kwargs["timeout"] == 900


def test_ci_consumer_retirement_retains_all_reviewed_mock_and_false_branch_contracts():
    retained = ['tests/test_certified_secure_host.py::test_certified_secure_host_report_runs_all_batch_db_suites',
     'tests/test_cockpit_efficiency_benchmark.py::test_cockpit_efficiency_benchmark_report_summarizes_successful_run',
     'tests/test_cockpit_efficiency_benchmark.py::test_cockpit_efficiency_benchmark_report_surfaces_failures_without_overclaiming',
     'tests/test_container_grade_secure_host.py::test_container_grade_secure_host_report_runs_all_batch_ct_suites',
     'tests/test_guardian_benchmark.py::test_guardian_user_model_benchmark_report_reflects_suite_failures',
     'tests/test_guardian_benchmark.py::test_guardian_user_model_benchmark_report_stays_ci_gated_when_suite_passes',
     'tests/test_independent_secure_host_review.py::test_independent_secure_host_review_report_runs_all_batch_ck_suites',
     'tests/test_live_long_horizon_replay_benchmark.py::test_live_replay_benchmark_report_summarizes_success_and_failures',
     'tests/test_m6_memory_superiority_benchmark.py::test_m6_memory_superiority_benchmark_report_reflects_suite_failures',
     'tests/test_m6_memory_superiority_benchmark.py::test_m6_memory_superiority_benchmark_report_stays_ci_gated_when_suite_passes',
     'tests/test_m8_guardian_brain.py::test_m8_guardian_brain_benchmark_report_reflects_suite_failures',
     'tests/test_m8_guardian_brain.py::test_m8_guardian_brain_benchmark_report_stays_ci_gated_when_suite_passes',
     'tests/test_memory_benchmark.py::test_guardian_memory_benchmark_report_exposes_gate_a_baseline_receipt',
     'tests/test_memory_benchmark.py::test_guardian_memory_benchmark_report_marks_embedded_mode_as_not_run',
     'tests/test_memory_benchmark.py::test_guardian_memory_benchmark_report_reflects_suite_failures',
     'tests/test_memory_benchmark.py::test_guardian_memory_benchmark_report_stays_ci_gated_when_suite_passes',
     'tests/test_memory_provider_quality_gate.py::test_memory_provider_quality_gate_report_reflects_suite_failures',
     'tests/test_memory_provider_quality_gate.py::test_memory_provider_quality_gate_report_stays_ci_gated_when_suite_passes',
     'tests/test_memory_providers.py::test_memory_provider_inventory_endpoint_lists_configured_additive_provider',
     'tests/test_memory_providers.py::test_memory_provider_inventory_surfaces_capability_governance_states',
     'tests/test_memory_providers.py::test_plan_memory_retrieval_tolerates_provider_health_failures',
     'tests/test_operator_api.py::test_operator_cockpit_efficiency_benchmark_surface_degrades_summary_on_failures',
     'tests/test_operator_api.py::test_operator_computer_use_benchmark_surface_degrades_summary_on_failures',
     'tests/test_operator_api.py::test_operator_durable_workflow_engine_surface_delegates_to_state_report',
     'tests/test_operator_api.py::test_operator_durable_workflow_engine_v2_surface_delegates_to_report',
     'tests/test_operator_api.py::test_operator_live_replay_benchmark_surface_degrades_summary_on_failures',
     'tests/test_operator_api.py::test_operator_live_workflow_endurance_canary_surface_degrades_summary_on_failures',
     'tests/test_operator_api.py::test_operator_m6_memory_superiority_surface_delegates_to_memory_payload',
     'tests/test_operator_api.py::test_operator_m7_cockpit_composes_dense_control_surface',
     'tests/test_operator_api.py::test_operator_memory_provider_quality_gate_surface_degrades_summary_on_failures',
     'tests/test_operator_api.py::test_operator_one_reach_channel_canary_surface_degrades_summary_on_failures',
     'tests/test_operator_api.py::test_operator_trust_boundary_benchmark_surface_degrades_summary_on_failures',
     'tests/test_operator_api.py::test_operator_workflow_endurance_benchmark_surface_degrades_summary_on_failures',
     'tests/test_post_dp_durable_orchestration.py::test_post_dp_durable_orchestration_report_degrades_on_failures',
     'tests/test_post_dp_durable_orchestration.py::test_post_dp_durable_orchestration_report_ignores_unrelated_persisted_runs',
     'tests/test_post_dp_durable_orchestration.py::test_post_dp_durable_orchestration_report_keeps_receipt_story_on_pass',
     'tests/test_post_dp_operator_debugging_recovery.py::test_post_dp_operator_debugging_recovery_report_exposes_scenario_names',
     'tests/test_post_dp_reach_channel_gap_closure.py::test_post_dp_reach_channel_report_runs_all_ds_suites',
     'tests/test_post_dp_secure_host_gap_closure.py::test_post_dp_secure_host_report_runs_all_dr_suites',
     'tests/test_post_dx_formal_secure_runtime_isolation.py::test_post_dx_formal_secure_runtime_report_runs_all_dz_suites',
     'tests/test_post_dx_reach_voice_media_parity.py::test_post_dx_reach_voice_media_report_runs_all_suites',
     'tests/test_production_grade_secure_host.py::test_production_grade_secure_host_report_runs_all_dj_suites',
     'tests/test_production_isolation.py::test_production_isolation_security_report_runs_all_batch_cd_suites',
     'tests/test_workflow_benchmark.py::test_live_workflow_endurance_canary_report_degrades_on_failures',
     'tests/test_workflow_benchmark.py::test_live_workflow_endurance_canary_report_keeps_receipt_story_on_pass',
     'tests/test_workflow_benchmark.py::test_workflow_endurance_benchmark_report_degrades_summary_states_on_failures',
     'tests/test_workflow_benchmark.py::test_workflow_endurance_benchmark_report_keeps_healthy_summary_states_on_pass']
    retired = {node for nodes in CANCELLED_EVAL_CONSUMER_NODES.values() for node in nodes}
    assert len(retained) == 47
    assert retired.isdisjoint(retained)


def test_fully_retired_group_is_fixed_and_current_source_has_no_ordinary_selected_definition():
    assert FULLY_RETIRED_EVAL_INVOCATIONS == {
        ("tests/test_operator_api.py", "tests/test_operator_api.py::marketplace_and_ecosystem"),
    }
    root = Path(__file__).resolve().parents[1]
    for path, label in FULLY_RETIRED_EVAL_INVOCATIONS:
        args = dict(pytest_invocations_for_target(path))[label]
        expression = args[args.index("-k") + 1]
        definitions = [node.name for node in ast.parse((root / path).read_text()).body
                       if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test_")]
        def matches(name):
            tokens = re.findall(r"\w+|[()]", expression)
            parsed = ast.parse(" ".join(token if token in {"and", "or", "not", "(", ")"}
                                       else str(token.lower() in (path + "::" + name).lower())
                                       for token in tokens), mode="eval")
            def evaluate(node):
                if isinstance(node, ast.Expression):
                    return evaluate(node.body)
                if isinstance(node, ast.Constant) and type(node.value) is bool:
                    return node.value
                if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
                    return not evaluate(node.operand)
                if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.And):
                    return all(evaluate(value) for value in node.values)
                if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.Or):
                    return any(evaluate(value) for value in node.values)
                raise AssertionError("Unsupported source predicate")
            return evaluate(parsed)
        selected = {path + "::" + name for name in definitions if matches(name)}
        assert len(selected) == 10
        assert selected <= set(CANCELLED_EVAL_CONSUMER_NODES[path])


def test_fully_retired_group_skips_before_subprocess_and_reports_not_run(tmp_path, capsys):
    path, label = next(iter(FULLY_RETIRED_EVAL_INVOCATIONS))
    with patch("scripts.run_backend_test_shard.pytest_invocations_for_target", return_value=[(label, [path])]), patch("scripts.run_backend_test_shard.subprocess.run") as run:
        assert run_shard_files(tmp_path, [path], exclude_cancelled_eval_harness=True) == 0
    run.assert_not_called()
    assert f"RETIRED/NOT RUN {label}:" in capsys.readouterr().out


def test_fully_retired_group_does_not_skip_similar_label(tmp_path):
    path, label = next(iter(FULLY_RETIRED_EVAL_INVOCATIONS))
    with patch("scripts.run_backend_test_shard.pytest_invocations_for_target", return_value=[(label + "_ordinary", [path])]), patch("scripts.run_backend_test_shard.subprocess.run") as run:
        run.return_value = CompletedProcess(args=["pytest"], returncode=0)
        assert run_shard_files(tmp_path, [path], exclude_cancelled_eval_harness=True) == 0
    run.assert_called_once()


def test_fully_retired_group_default_runner_still_invokes_original_groups(tmp_path):
    path, label = next(iter(FULLY_RETIRED_EVAL_INVOCATIONS))
    with patch("scripts.run_backend_test_shard.subprocess.run") as run:
        run.return_value = CompletedProcess(args=["pytest"], returncode=0)
        assert run_shard_files(tmp_path, [path]) == 0
    assert len(run.call_args_list) == len(pytest_invocations_for_target(path))
    expected = [args for _, args in pytest_invocations_for_target(path)]
    assert [call.args[0][4:-1] for call in run.call_args_list] == expected


def test_ci_retirement_does_not_swallow_no_tests_exit_code(tmp_path):
    with patch("scripts.run_backend_test_shard.subprocess.run") as run:
        run.return_value = CompletedProcess(args=["pytest"], returncode=5)
        assert run_shard_files(tmp_path, ["tests/test_operator_api.py", "tests/test_alpha.py"], exclude_cancelled_eval_harness=True) == 5
    run.assert_called_once()
