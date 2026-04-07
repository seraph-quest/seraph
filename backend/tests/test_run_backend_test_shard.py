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
    assert timeout_for_file("tests/test_alpha.py", 900) == 900


def test_pytest_invocations_for_target_splits_eval_harness_contract():
    invocations = pytest_invocations_for_target("tests/test_eval_harness.py")

    assert invocations == [
        (
            "tests/test_eval_harness.py::test_run_runtime_evals_passes_all_scenarios",
            ["tests/test_eval_harness.py::test_run_runtime_evals_passes_all_scenarios"],
        ),
        (
            "tests/test_eval_harness.py::remaining",
            [
                "tests/test_eval_harness.py",
                "-k",
                "not test_run_runtime_evals_passes_all_scenarios",
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
        ]

        result = run_shard_files(tmp_path, files, file_timeout_seconds=900)

    assert result == 0
    assert mock_run.call_count == 2
    first_command = mock_run.call_args_list[0].args[0]
    second_command = mock_run.call_args_list[1].args[0]
    assert "tests/test_eval_harness.py::test_run_runtime_evals_passes_all_scenarios" in first_command
    assert second_command[4:7] == [
        "tests/test_eval_harness.py",
        "-k",
        "not test_run_runtime_evals_passes_all_scenarios",
    ]
    assert mock_run.call_args_list[0].kwargs["timeout"] == 1_500


def test_pytest_invocations_for_target_splits_workflows_contract():
    invocations = pytest_invocations_for_target("tests/test_workflows.py")

    assert invocations == [
        (
            "tests/test_workflows.py::boundary_drift",
            [
                "tests/test_workflows.py",
                "-k",
                "approval_context or authenticated_source or delegated_specialist or delegated_tool_inventory or legacy_checkpoint",
            ],
        ),
        (
            "tests/test_workflows.py::remaining",
            [
                "tests/test_workflows.py",
                "-k",
                "not (approval_context or authenticated_source or delegated_specialist or delegated_tool_inventory or legacy_checkpoint)",
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
