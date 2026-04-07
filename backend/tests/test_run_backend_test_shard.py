from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch

from scripts.run_backend_test_shard import run_shard_files


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
    assert first_call.args[0][0].endswith("python")
    assert first_call.args[0][1:4] == ["-m", "pytest", "-q"]
    assert "tests/test_alpha.py" in first_call.args[0]
    assert "-x" in first_call.args[0]
    assert "tests/test_beta.py" in second_call.args[0]
    assert "-x" in second_call.args[0]


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
