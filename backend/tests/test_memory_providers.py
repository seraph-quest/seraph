from __future__ import annotations

import json
from dataclasses import dataclass
from unittest.mock import patch

import pytest

from config.settings import settings
from src.api.memory import list_memory_providers
from src.api.router import api_router
from src.extensions.lifecycle import (
    configure_extension,
    list_extension_connectors,
    set_extension_connector_enabled,
)
from src.extensions.registry import _current_seraph_version
from src.memory.hybrid_retrieval import HybridMemoryRetrievalResult
from src.memory.providers import (
    MemoryProviderHit,
    MemoryProviderRetrievalResult,
    clear_memory_provider_adapters,
    register_memory_provider_adapter,
)
from src.memory.retrieval_planner import plan_memory_retrieval


@dataclass
class FakeMemoryProviderAdapter:
    name: str = "graph-memory"
    provider_kind: str = "vector_plugin"
    capabilities: tuple[str, ...] = ("retrieval",)
    degraded: bool = False
    should_fail: bool = False

    def health(self) -> dict[str, object]:
        return {
            "status": "degraded" if self.degraded else "ready",
            "summary": "External memory plugin is connected.",
        }

    async def retrieve(self, *, query: str, active_projects: tuple[str, ...] = (), limit: int = 4, config=None):
        if self.should_fail:
            raise RuntimeError("provider down")
        return MemoryProviderRetrievalResult(
            hits=(
                MemoryProviderHit(
                    text=f"Provider recall for {query}",
                    score=0.61,
                    provider_name=self.name,
                ),
            ),
            degraded=self.degraded,
            summary="Provider-assisted retrieval available.",
        )


@dataclass
class ExplodingHealthMemoryProviderAdapter(FakeMemoryProviderAdapter):
    def health(self) -> dict[str, object]:
        raise RuntimeError("health unavailable")


def _write_memory_provider_extension(workspace, *, enabled: bool = True, configured: bool = True) -> None:
    pack = workspace / "extensions" / "graph-memory-pack"
    (pack / "connectors" / "memory").mkdir(parents=True)
    current_version = _current_seraph_version()
    pack.joinpath("manifest.yaml").write_text(
        "id: seraph.graph-memory-pack\n"
        f"version: {current_version}\n"
        "display_name: Graph Memory Pack\n"
        "kind: connector-pack\n"
        "compatibility:\n"
        f"  seraph: \">={current_version}\"\n"
        "publisher:\n"
        "  name: Seraph\n"
        "trust: local\n"
        "contributes:\n"
        "  memory_providers:\n"
        "    - connectors/memory/graph-memory.yaml\n",
        encoding="utf-8",
    )
    pack.joinpath("connectors", "memory", "graph-memory.yaml").write_text(
        "name: graph-memory\n"
        "description: Additive retrieval provider.\n"
        "provider_kind: vector_plugin\n"
        f"enabled: {'true' if enabled else 'false'}\n"
        "capabilities:\n"
        "  - retrieval\n"
        "canonical_memory_owner: seraph\n"
        "canonical_write_mode: additive_only\n"
        "config_fields:\n"
        "  - key: api_key\n"
        "    label: API Key\n"
        "    input: password\n"
        "    required: true\n",
        encoding="utf-8",
    )
    state = {
        "extensions": {
            "seraph.graph-memory-pack": {
                "config": {
                    "memory_providers": {
                        "graph-memory": ({"api_key": "secret"} if configured else {})
                    }
                },
                "connector_state": {
                    "connectors/memory/graph-memory.yaml": {"enabled": enabled},
                },
            }
        }
    }
    workspace.joinpath("extensions-state.json").write_text(json.dumps(state), encoding="utf-8")


@pytest.mark.asyncio
async def test_memory_provider_inventory_endpoint_lists_configured_additive_provider(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _write_memory_provider_extension(workspace)
    adapter = FakeMemoryProviderAdapter()
    register_memory_provider_adapter(adapter)
    try:
        with patch.object(settings, "workspace_dir", str(workspace)):
            payload = await list_memory_providers()
    finally:
        clear_memory_provider_adapters()

    provider = payload["providers"][0]
    assert provider["name"] == "graph-memory"
    assert provider["provider_kind"] == "vector_plugin"
    assert provider["runtime_state"] == "ready"
    assert provider["canonical_memory_owner"] == "seraph"
    assert provider["canonical_write_mode"] == "additive_only"
    assert "Canonical guardian memory remains authoritative" in provider["notes"][0]


def test_memory_provider_inventory_route_is_registered():
    assert any(route.path == "/api/memory/providers" for route in api_router.routes)


def test_memory_provider_lifecycle_supports_config_and_enable(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _write_memory_provider_extension(workspace, enabled=False, configured=False)

    with patch.object(settings, "workspace_dir", str(workspace)):
        connectors_before = list_extension_connectors("seraph.graph-memory-pack")
        connector = connectors_before["connectors"][0]
        assert connector["type"] == "memory_providers"
        assert connector["name"] == "graph-memory"
        assert connector["enabled"] is False
        assert connector["configured"] is False
        assert connector["status"] == "requires_config"
        assert connector["health"]["state"] == "requires_config"

        configured = configure_extension(
            "seraph.graph-memory-pack",
            {"memory_providers": {"graph-memory": {"api_key": "secret"}}},
        )
        configured_connector = next(
            item for item in configured["contributions"] if item["reference"] == "connectors/memory/graph-memory.yaml"
        )
        assert configured["config"]["memory_providers"]["graph-memory"]["api_key"] == "__SERAPH_STORED_SECRET__"
        assert configured_connector["configured"] is True
        assert configured_connector["status"] == "disabled"

        enabled = set_extension_connector_enabled(
            "seraph.graph-memory-pack",
            "connectors/memory/graph-memory.yaml",
            enabled=True,
        )
        assert enabled["changed"]["type"] == "memory_provider"
        assert enabled["changed"]["enabled"] is True

        connectors_after = list_extension_connectors("seraph.graph-memory-pack")
        assert connectors_after["connectors"][0]["enabled"] is True


@pytest.mark.asyncio
async def test_plan_memory_retrieval_merges_provider_hits_without_overriding_canonical(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _write_memory_provider_extension(workspace)
    adapter = FakeMemoryProviderAdapter()
    register_memory_provider_adapter(adapter)
    try:
        with (
            patch.object(settings, "workspace_dir", str(workspace)),
            patch(
                "src.memory.retrieval_planner.build_structured_memory_context_bundle",
                return_value=(" - [goal] Ship Batch N".replace(" -", "-"), {"goal": ("Ship Batch N",)}),
            ),
            patch(
                "src.memory.retrieval_planner.retrieve_hybrid_memory",
                return_value=HybridMemoryRetrievalResult(
                    context="- [project] Memory roadmap",
                    buckets={"project": ("Memory roadmap",)},
                    degraded=False,
                    hits=(),
                ),
            ),
        ):
            retrieval = await plan_memory_retrieval(query="memory roadmap", active_projects=("Memory roadmap",))
    finally:
        clear_memory_provider_adapters()

    assert retrieval.lane == "hybrid_plus_provider"
    assert "Ship Batch N" in retrieval.semantic_context
    assert "Provider recall for memory roadmap" in retrieval.semantic_context
    assert retrieval.memory_buckets["external_memory"] == ("Provider recall for memory roadmap",)
    assert retrieval.provider_diagnostics[0]["name"] == "graph-memory"
    assert retrieval.provider_diagnostics[0]["runtime_state"] == "ready"


@pytest.mark.asyncio
async def test_plan_memory_retrieval_degrades_cleanly_when_provider_fails(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _write_memory_provider_extension(workspace)
    adapter = FakeMemoryProviderAdapter(should_fail=True)
    register_memory_provider_adapter(adapter)
    try:
        with (
            patch.object(settings, "workspace_dir", str(workspace)),
            patch(
                "src.memory.retrieval_planner.build_structured_memory_context_bundle",
                return_value=("- [goal] Keep canonical memory first", {"goal": ("Keep canonical memory first",)}),
            ),
            patch(
                "src.memory.retrieval_planner.retrieve_hybrid_memory",
                return_value=HybridMemoryRetrievalResult(
                    context="",
                    buckets={},
                    degraded=False,
                    hits=(),
                ),
            ),
        ):
            retrieval = await plan_memory_retrieval(query="canonical memory", active_projects=())
    finally:
        clear_memory_provider_adapters()

    assert retrieval.degraded is False
    assert retrieval.lane == "hybrid"
    assert "Keep canonical memory first" in retrieval.semantic_context
    assert "Provider recall" not in retrieval.semantic_context
    assert retrieval.provider_diagnostics[0]["runtime_state"] == "unavailable"


@pytest.mark.asyncio
async def test_plan_memory_retrieval_tolerates_provider_health_failures(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _write_memory_provider_extension(workspace)
    adapter = ExplodingHealthMemoryProviderAdapter()
    register_memory_provider_adapter(adapter)
    try:
        with (
            patch.object(settings, "workspace_dir", str(workspace)),
            patch(
                "src.memory.retrieval_planner.build_structured_memory_context_bundle",
                return_value=("- [goal] Canonical memory still available", {"goal": ("Canonical memory still available",)}),
            ),
            patch(
                "src.memory.retrieval_planner.retrieve_hybrid_memory",
                return_value=HybridMemoryRetrievalResult(
                    context="- [project] Canonical recall",
                    buckets={"project": ("Canonical recall",)},
                    degraded=False,
                    hits=(),
                ),
            ),
        ):
            retrieval = await plan_memory_retrieval(query="canonical recall", active_projects=())
            inventory = await list_memory_providers()
    finally:
        clear_memory_provider_adapters()

    assert retrieval.degraded is False
    assert retrieval.lane == "hybrid"
    assert "Canonical memory still available" in retrieval.semantic_context
    assert retrieval.provider_diagnostics == ()
    assert inventory["providers"][0]["runtime_state"] == "unavailable"
