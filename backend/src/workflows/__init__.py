"""Workflow composition support."""

from __future__ import annotations

from src.workflows.loader import Workflow, WorkflowStep, load_workflows

__all__ = ["Workflow", "WorkflowManager", "WorkflowStep", "load_workflows", "workflow_manager"]


def __getattr__(name: str):
    if name in {"WorkflowManager", "workflow_manager"}:
        from src.workflows.manager import WorkflowManager, workflow_manager

        if name == "WorkflowManager":
            return WorkflowManager
        return workflow_manager
    raise AttributeError(name)
