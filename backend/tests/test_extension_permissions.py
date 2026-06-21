from unittest.mock import MagicMock, patch

from src.extensions.capability_contract import build_capability_contract
from src.extensions.manifest import parse_extension_manifest
from src.extensions.permissions import evaluate_contribution_permissions, evaluate_tool_permissions
from src.extensions.registry import ExtensionRecord


def test_evaluate_tool_permissions_preserves_runtime_mcp_secret_ref_fields():
    mcp_tool = MagicMock()
    mcp_tool.name = "mcp_tasks"
    mcp_tool.inputs = {
        "headers": {"type": "object", "description": "Authentication headers"},
        "body": {"type": "string", "description": "Request body"},
    }

    with patch("src.extensions.permissions.mcp_manager.get_tools", return_value=[mcp_tool]):
        permissions = evaluate_tool_permissions(None, tool_names=["mcp_tasks"])

    assert permissions["accepts_secret_refs"] is True


def _extension_record_for_manifest(manifest_content: str) -> ExtensionRecord:
    manifest = parse_extension_manifest(manifest_content)
    return ExtensionRecord(
        id=manifest.id,
        display_name=manifest.display_name,
        kind=manifest.kind.value,
        trust=manifest.trust.value,
        source="manifest",
        root_path="/tmp/contract-pack",
        manifest_path="/tmp/contract-pack/manifest.yaml",
        manifest=manifest,
    )


def test_capability_contract_rejects_undeclared_privileged_workflow_behavior():
    extension = _extension_record_for_manifest(
        """
id: seraph.contract-test
version: 2026.5.4
display_name: Contract Test
kind: capability-pack
compatibility:
  seraph: ">=2026.4.11"
publisher:
  name: Seraph
trust: local
description: Operator-visible pack description.
contributes:
  workflows:
    - workflows/write-note.md
permissions:
  tools: []
  network: false
  data_access:
    - workspace.files
  mutation_rights:
    - none
  audit_events:
    - tool_call
"""
    )

    profile = evaluate_contribution_permissions(
        extension,
        contribution_type="workflows",
        metadata={"step_tools": ["write_file"], "description": "Writes into the workspace."},
    )
    contract = build_capability_contract(
        extension,
        contribution_type="workflows",
        reference="workflows/write-note.md",
        metadata={"step_tools": ["write_file"], "description": "Writes into the workspace."},
        permission_profile=profile,
    )

    assert contract["schema_version"] == "2026-05-04.m1"
    assert contract["provenance"]["extension_id"] == "seraph.contract-test"
    assert contract["operator"]["description"] == "Writes into the workspace."
    assert contract["permissions"]["declared"]["data_access"] == ["workspace.files"]
    assert contract["permissions"]["declared"]["mutation_rights"] == ["none"]
    assert contract["permissions"]["declared"]["audit_events"] == ["tool_call"]
    assert contract["permissions"]["missing"]["tools"] == ["write_file"]
    assert contract["permissions"]["missing"]["execution_boundaries"] == ["workspace_write"]
    assert contract["enforcement"]["status"] == "rejected"
    assert contract["enforcement"]["action"] == "reject"
    assert contract["enforcement"]["runtime_ready"] is False


def test_capability_contract_quarantines_undeclared_network_connector_behavior():
    extension = _extension_record_for_manifest(
        """
id: seraph.connector-contract-test
version: 2026.5.4
display_name: Connector Contract Test
kind: connector-pack
compatibility:
  seraph: ">=2026.4.11"
publisher:
  name: Seraph
trust: local
contributes:
  managed_connectors:
    - connectors/managed/github.yaml
permissions:
  network: false
"""
    )

    profile = evaluate_contribution_permissions(
        extension,
        contribution_type="managed_connectors",
        metadata={"name": "github", "requires_network": True},
    )
    contract = build_capability_contract(
        extension,
        contribution_type="managed_connectors",
        reference="connectors/managed/github.yaml",
        metadata={"name": "github", "requires_network": True},
        permission_profile=profile,
    )

    assert contract["permissions"]["missing"]["network"] is True
    assert contract["enforcement"]["status"] == "quarantined"
    assert contract["enforcement"]["action"] == "quarantine"


def test_capability_contract_rejects_mcp_server_without_declared_boundary():
    extension = _extension_record_for_manifest(
        """
id: seraph.mcp-contract-test
version: 2026.5.4
display_name: MCP Contract Test
kind: connector-pack
compatibility:
  seraph: ">=2026.4.11"
publisher:
  name: Seraph
trust: local
contributes:
  mcp_servers:
    - mcp/github.json
permissions:
  network: true
"""
    )

    profile = evaluate_contribution_permissions(
        extension,
        contribution_type="mcp_servers",
        metadata={"name": "github", "transport": "stdio"},
    )
    contract = build_capability_contract(
        extension,
        contribution_type="mcp_servers",
        reference="mcp/github.json",
        metadata={"name": "github", "transport": "stdio"},
        permission_profile=profile,
    )

    assert contract["permissions"]["missing"]["execution_boundaries"] == ["external_mcp"]
    assert contract["permissions"]["missing"]["network"] is False
    assert contract["enforcement"]["status"] == "rejected"
    assert contract["enforcement"]["action"] == "reject"


def test_messaging_connector_requires_external_channel_policy_behavior():
    extension = _extension_record_for_manifest(
        """
id: seraph.messaging-contract-test
version: 2026.5.5
display_name: Messaging Contract Test
kind: connector-pack
compatibility:
  seraph: ">=2026.4.11"
publisher:
  name: Seraph
trust: local
contributes:
  messaging_connectors:
    - connectors/messaging/signal.yaml
permissions:
  network: true
"""
    )

    profile = evaluate_contribution_permissions(
        extension,
        contribution_type="messaging_connectors",
        metadata={"name": "Signal", "requires_network": True},
    )

    assert profile["ok"] is True
    assert profile["risk_level"] == "medium"
    assert profile["requires_approval"] is True
    assert profile["approval_behavior"] == "external_channel_policy"
    assert profile["lifecycle_approval_boundaries"] == ["external_channel"]


def test_node_adapter_requires_device_pairing_policy_behavior():
    extension = _extension_record_for_manifest(
        """
id: seraph.node-contract-test
version: 2026.5.5
display_name: Node Contract Test
kind: connector-pack
compatibility:
  seraph: ">=2026.4.11"
publisher:
  name: Seraph
trust: local
contributes:
  node_adapters:
    - connectors/nodes/companion.yaml
permissions:
  network: true
"""
    )

    profile = evaluate_contribution_permissions(
        extension,
        contribution_type="node_adapters",
        metadata={"name": "Companion device", "requires_network": True},
    )

    assert profile["ok"] is True
    assert profile["risk_level"] == "medium"
    assert profile["requires_approval"] is True
    assert profile["approval_behavior"] == "device_pairing_policy"
    assert profile["lifecycle_approval_boundaries"] == ["device_pairing"]
