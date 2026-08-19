"""Pure workspace state inspection contracts."""

from src.workspace.state_registry import (
    AmbiguousWorkspaceRootsError,
    ExternalReferencePolicy,
    ExternalReferenceSpec,
    UnknownWorkspacePathError,
    UnsupportedWorkspaceEntryError,
    WorkspaceConfig,
    WorkspaceDatabaseObjectSpec,
    WorkspaceIdentity,
    WorkspacePathSpec,
    WorkspaceRootKind,
    WorkspaceStateClass,
    WorkspaceStateError,
    WorkspaceStateRegistry,
)

__all__ = [
    "AmbiguousWorkspaceRootsError",
    "ExternalReferencePolicy",
    "ExternalReferenceSpec",
    "UnknownWorkspacePathError",
    "UnsupportedWorkspaceEntryError",
    "WorkspaceConfig",
    "WorkspaceDatabaseObjectSpec",
    "WorkspaceIdentity",
    "WorkspacePathSpec",
    "WorkspaceRootKind",
    "WorkspaceStateClass",
    "WorkspaceStateError",
    "WorkspaceStateRegistry",
]
