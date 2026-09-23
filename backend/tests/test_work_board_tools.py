"""Provider-free proofs for bound worker-only board controls."""

import pytest
from pydantic import ValidationError

from src.native_tools.loader import reload_tools
from src.tools.work_board_tools import (
    WorkBoardWorkerHost,
    bind_work_board_worker_context,
    get_bound_work_board_tools,
    work_board_show,
)
from src.work_board.tools import WorkBoardWorkerBlock, WorkBoardWorkerEvidence, WorkBoardWorkerRequest


def _request() -> WorkBoardWorkerRequest:
    return WorkBoardWorkerRequest(
        task_id="task-1",
        attempt_id="attempt-1",
        expected_task_revision=3,
        board_fencing_token=2,
        workflow_run_id="work-board:task-1:attempt-1",
        workflow_fencing_token=4,
    )


def test_general_native_discovery_does_not_expose_board_worker_controls():
    assert not any(tool.name.startswith("work_board_") for tool in reload_tools())


def test_worker_controls_fail_closed_without_server_trust_binding():
    with pytest.raises(PermissionError):
        work_board_show("task-1")


def test_worker_tool_requests_use_closed_block_and_evidence_schemas():
    request = _request()
    assert WorkBoardWorkerBlock(**request.model_dump(), block_kind="unknown_effect").block_kind == "unknown_effect"
    with pytest.raises(ValidationError):
        WorkBoardWorkerBlock(**request.model_dump(), block_kind="arbitrary_private_reason")
    with pytest.raises(ValidationError):
        WorkBoardWorkerEvidence(**request.model_dump(), evidence_refs=["/private/source"])
    with pytest.raises(ValidationError):
        WorkBoardWorkerEvidence(**request.model_dump(), evidence_refs=[f"artifact:{i}" for i in range(21)])


def test_bound_worker_host_exposes_only_the_six_scoped_controls():
    host = WorkBoardWorkerHost(_request())
    assert [tool.name for tool in host.tools] == [
        "work_board_show",
        "work_board_heartbeat",
        "work_board_comment",
        "work_board_block",
        "work_board_request_review",
        "work_board_completion_request",
    ]
    assert all("create" not in tool.name and "link" not in tool.name and "unblock" not in tool.name for tool in host.tools)


def test_bound_worker_context_is_the_only_runtime_injection_path():
    assert get_bound_work_board_tools() == []
    with bind_work_board_worker_context(_request()):
        assert [tool.name for tool in get_bound_work_board_tools()] == [
            "work_board_show",
            "work_board_heartbeat",
            "work_board_comment",
            "work_board_block",
            "work_board_request_review",
            "work_board_completion_request",
        ]
    assert get_bound_work_board_tools() == []
