from threading import Event
from unittest.mock import MagicMock

import pytest

from src.auth.cancellation import RuntimeRevokedError, reset_revocation_guard, set_revocation_guard
from src.tools.audit import AuditedTool
from src.tools.audit import wrap_tools_for_audit
from src.tools.approval import wrap_tools_for_approval
import inspect
from src.agent import factory, specialists


def test_revoked_executor_guard_blocks_wrapped_tool_before_side_effect():
    wrapped = MagicMock()
    wrapped.name = "mutating_tool"
    wrapped.description = "mutation sentinel"
    wrapped.inputs = {}
    wrapped.output_type = "string"
    tool = AuditedTool(wrapped)
    guard = Event()
    token = set_revocation_guard(guard)
    guard.set()
    try:
        with pytest.raises(RuntimeRevokedError):
            tool()
    finally:
        reset_revocation_guard(token)
    wrapped.assert_not_called()


def test_low_risk_specialist_workflow_is_audited_and_cancellation_aware():
    raw = MagicMock()
    raw.name = "workflow_low_risk_sentinel"
    raw.description = "low risk workflow"
    raw.inputs = {}
    raw.output_type = "string"
    assembled = wrap_tools_for_approval(
        wrap_tools_for_audit([raw]),
        risk_overrides={raw.name: "low"},
    )
    assert len(assembled) == 1
    assert isinstance(assembled[0], AuditedTool)

    guard = Event()
    token = set_revocation_guard(guard)
    guard.set()
    try:
        with pytest.raises(RuntimeRevokedError):
            assembled[0]()
    finally:
        reset_revocation_guard(token)
    raw.assert_not_called()


def test_normal_and_specialist_workflow_assembly_audits_before_approval():
    normal_source = inspect.getsource(factory._append_workflow_tools)
    specialist_source = inspect.getsource(specialists.build_all_specialists)
    assert "wrap_tools_for_audit" in normal_source
    assert "wrap_tools_for_audit" in specialist_source
    assert specialist_source.index("workflow_tools = wrap_tools_for_audit") < specialist_source.index(
        "workflow_tools = wrap_tools_for_approval"
    )
