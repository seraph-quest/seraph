from __future__ import annotations

import json
from dataclasses import dataclass
from dataclasses import field
from datetime import datetime, timedelta, timezone
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
    MemoryProviderWritebackResult,
    clear_memory_provider_adapters,
    register_memory_provider_adapter,
    writeback_additive_memory_providers,
)
from src.memory.retrieval_planner import plan_memory_retrieval
from src.memory.types import ConsolidatedMemoryItem, kind_to_category, normalize_memory_kind


@dataclass
class FakeMemoryProviderAdapter:
    name: str = "graph-memory"
    provider_kind: str = "vector_plugin"
    capabilities: tuple[str, ...] = ("retrieval",)
    degraded: bool = False
    should_fail: bool = False
    model_should_fail: bool = False
    writeback_should_fail: bool = False
    model_hits: tuple[MemoryProviderHit, ...] = ()
    writeback_calls: list[dict[str, object]] = field(default_factory=list)

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

    async def augment_model(self, *, active_projects: tuple[str, ...] = (), limit: int = 4, config=None):
        if self.model_should_fail:
            raise RuntimeError("provider model down")
        if self.model_hits:
            hits = self.model_hits[:limit]
        else:
            active_project = active_projects[0] if active_projects else "current work"
            hits = (
                MemoryProviderHit(
                    text=f"{active_project} remains the live project anchor.",
                    score=0.63,
                    provider_name=self.name,
                    bucket="project",
                ),
                MemoryProviderHit(
                    text=f"Alice owns {active_project} communications.",
                    score=0.72,
                    provider_name=self.name,
                    bucket="collaborator",
                ),
            )
        return MemoryProviderRetrievalResult(
            hits=hits,
            degraded=self.degraded,
            summary="Provider-backed user model available.",
        )

    async def writeback(
        self,
        *,
        memories: tuple[ConsolidatedMemoryItem, ...],
        session_id: str,
        trigger: str,
        workflow_name: str | None = None,
        config=None,
    ):
        self.writeback_calls.append(
            {
                "session_id": session_id,
                "trigger": trigger,
                "workflow_name": workflow_name,
                "kinds": [memory.kind.value for memory in memories],
                "texts": [memory.text for memory in memories],
            }
        )
        if self.writeback_should_fail:
            raise RuntimeError("provider writeback down")
        return MemoryProviderWritebackResult(
            stored_count=len(memories),
            summary="Provider writeback stored canonical memory copies.",
            accepted_kinds=tuple(sorted({memory.kind.value for memory in memories})),
        )


@dataclass
class ExplodingHealthMemoryProviderAdapter(FakeMemoryProviderAdapter):
    def health(self) -> dict[str, object]:
        raise RuntimeError("health unavailable")


def _write_memory_provider_extension(
    workspace,
    *,
    enabled: bool = True,
    configured: bool = True,
    capabilities: tuple[str, ...] = ("retrieval",),
) -> None:
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
        (
            "name: graph-memory\n"
            "description: Additive retrieval provider.\n"
            "provider_kind: vector_plugin\n"
            f"enabled: {'true' if enabled else 'false'}\n"
            "capabilities:\n"
            + "".join(f"  - {capability}\n" for capability in capabilities)
            + "canonical_memory_owner: seraph\n"
            + "canonical_write_mode: additive_only\n"
            + "config_fields:\n"
            + "  - key: api_key\n"
            + "    label: API Key\n"
            + "    input: password\n"
            + "    required: true\n"
        ),
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
    assert provider["memory_contract"]["authoritative_memory"] == "guardian"
    assert provider["memory_contract"]["provider_retrieval_provenance"] == "external_advisory"
    assert provider["memory_contract"]["provider_writeback_provenance"] == "provider_mirror"
    assert provider["adapter_model"]["adapter_kind"] == "provider_neutral_memory_adapter"
    assert provider["adapter_model"]["capability_contracts"]["retrieval"]["sync_policy"] == "read_augment_only"
    assert provider["adapter_model"]["capability_contracts"]["retrieval"]["state"] == "ready"
    assert "Canonical guardian memory remains authoritative" in provider["notes"][0]
    assert provider["capability_states"]["retrieval"] == "ready"
    assert provider["governance"]["authoritative_memory"] == "guardian"
    assert provider["governance"]["conflict_policy"] == "guardian_wins"
    assert provider["quality_controls"]["writeback_minimum_confidence"] == 0.55
    assert payload["canonical_memory_contract"]["reconciliation_policy"] == "canonical_first"
    assert payload["provenance_taxonomy"][0]["name"] == "guardian_canonical"
    assert payload["adapter_model_rules"][0].startswith("Every provider capability is modeled")
    assert payload["quality_controls"]["project_scoped_buckets"] == [
        "collaborator",
        "commitment",
        "obligation",
        "project",
        "routine",
        "timeline",
    ]


@pytest.mark.asyncio
async def test_memory_provider_inventory_surfaces_capability_governance_states(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _write_memory_provider_extension(workspace, capabilities=("retrieval", "user_model", "consolidation"))
    adapter = FakeMemoryProviderAdapter(capabilities=("retrieval", "user_model", "consolidation"))
    register_memory_provider_adapter(adapter)
    try:
        with patch.object(settings, "workspace_dir", str(workspace)):
            payload = await list_memory_providers()
    finally:
        clear_memory_provider_adapters()

    provider = payload["providers"][0]
    assert provider["capability_states"]["retrieval"] == "ready"
    assert provider["capability_states"]["user_model"] == "ready"
    assert provider["capability_states"]["consolidation"] == "ready"
    assert provider["governance"]["writeback_state"] == "ready"
    assert payload["summary"]["user_model_ready_count"] == 1
    assert payload["summary"]["consolidation_ready_count"] == 1


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
    assert retrieval.provider_diagnostics[0]["canonical_authority"] == "guardian"
    assert retrieval.provider_diagnostics[0]["provenance"] == "external_advisory"
    assert retrieval.provider_diagnostics[0]["sync_policy"] == "read_augment_only"
    assert retrieval.provider_diagnostics[0]["capability_contracts_used"]["retrieval"]["operation_mode"] == "augment_recall"
    assert retrieval.provider_diagnostics[0]["runtime_state"] == "ready"


@pytest.mark.asyncio
async def test_plan_memory_retrieval_uses_provider_user_model_for_active_project_without_query(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _write_memory_provider_extension(workspace, capabilities=("user_model",))
    adapter = FakeMemoryProviderAdapter(
        capabilities=("user_model",),
        model_hits=(
            MemoryProviderHit(
                text="Atlas launch remains the live project anchor.",
                score=0.71,
                provider_name="graph-memory",
                bucket="project",
            ),
            MemoryProviderHit(
                text="Alice owns Atlas launch communications.",
                score=0.83,
                provider_name="graph-memory",
                bucket="collaborator",
            ),
            MemoryProviderHit(
                text="Atlas launch timeline ends on Friday.",
                score=0.64,
                provider_name="graph-memory",
                bucket="timeline",
            ),
        ),
    )
    register_memory_provider_adapter(adapter)
    try:
        with (
            patch.object(settings, "workspace_dir", str(workspace)),
            patch(
                "src.memory.retrieval_planner.build_structured_memory_context_bundle",
                return_value=("- [goal] Keep Atlas moving", {"goal": ("Keep Atlas moving",)}),
            ),
        ):
            retrieval = await plan_memory_retrieval(query="", active_projects=("Atlas launch",))
    finally:
        clear_memory_provider_adapters()

    assert retrieval.lane == "structured_plus_provider_model"
    assert "Alice owns Atlas launch communications." in retrieval.semantic_context
    assert retrieval.memory_buckets["collaborator"] == ("Alice owns Atlas launch communications.",)
    assert retrieval.provider_diagnostics[0]["capabilities_used"] == ["user_model"]
    assert retrieval.provider_diagnostics[0]["bucket_counts"]["timeline"] == 1
    assert retrieval.provider_diagnostics[0]["provenance"] == "external_advisory"
    assert retrieval.provider_diagnostics[0]["capability_contracts_used"]["user_model"]["sync_policy"] == "advisory_model_overlay"


@pytest.mark.asyncio
async def test_plan_memory_retrieval_uses_query_matched_project_hint_for_provider_user_model(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _write_memory_provider_extension(workspace, capabilities=("user_model",))
    adapter = FakeMemoryProviderAdapter(
        capabilities=("user_model",),
        model_hits=(
            MemoryProviderHit(
                text="Atlas launch remains the live project anchor.",
                score=0.71,
                provider_name="graph-memory",
                bucket="project",
            ),
            MemoryProviderHit(
                text="Alice owns Atlas launch communications.",
                score=0.83,
                provider_name="graph-memory",
                bucket="collaborator",
            ),
        ),
    )
    register_memory_provider_adapter(adapter)
    try:
        with (
            patch.object(settings, "workspace_dir", str(workspace)),
            patch(
                "src.memory.retrieval_planner.build_structured_memory_context_bundle",
                return_value=(
                    "- [project] Atlas launch\n- [goal] Keep Atlas moving",
                    {"project": ("Atlas launch",), "goal": ("Keep Atlas moving",)},
                ),
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
            retrieval = await plan_memory_retrieval(
                query="What matters for Atlas today?",
                active_projects=(),
            )
    finally:
        clear_memory_provider_adapters()

    diagnostics = retrieval.provider_diagnostics[0]
    assert retrieval.lane == "hybrid_plus_provider_model"
    assert "Alice owns Atlas launch communications." in retrieval.semantic_context
    assert diagnostics["attempted_capabilities"] == ["user_model"]
    assert diagnostics["capabilities_used"] == ["user_model"]
    assert diagnostics["topic_matches"] == ["Atlas launch"]
    assert diagnostics["quality_state"] == "focused"


@pytest.mark.asyncio
async def test_plan_memory_retrieval_combines_retrieval_and_user_model_provider_context(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _write_memory_provider_extension(workspace, capabilities=("retrieval", "user_model"))
    adapter = FakeMemoryProviderAdapter(capabilities=("retrieval", "user_model"))
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
                    context="- [project] Atlas launch",
                    buckets={"project": ("Atlas launch",)},
                    degraded=False,
                    hits=(),
                ),
            ),
        ):
            retrieval = await plan_memory_retrieval(query="atlas launch", active_projects=("Atlas launch",))
    finally:
        clear_memory_provider_adapters()

    assert retrieval.lane == "hybrid_plus_provider_model"
    assert "Provider recall for atlas launch" in retrieval.semantic_context
    assert "Alice owns Atlas launch communications." in retrieval.semantic_context
    assert retrieval.provider_diagnostics[0]["capabilities_used"] == ["retrieval", "user_model"]


@pytest.mark.asyncio
async def test_plan_memory_retrieval_keeps_high_value_user_model_hits_when_retrieval_fills_limit(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _write_memory_provider_extension(workspace, capabilities=("retrieval", "user_model"))
    adapter = FakeMemoryProviderAdapter(capabilities=("retrieval", "user_model"))

    async def crowded_retrieve(*, query: str, active_projects: tuple[str, ...] = (), limit: int = 4, config=None):
        return MemoryProviderRetrievalResult(
            hits=tuple(
                MemoryProviderHit(
                    text=f"Provider recall item {index} for {query}",
                    score=0.4 + (index * 0.01),
                    provider_name="graph-memory",
                    bucket="project",
                )
                for index in range(8)
            ),
            summary="Provider-assisted retrieval available.",
        )

    async def high_value_model(*, active_projects: tuple[str, ...] = (), limit: int = 4, config=None):
        return MemoryProviderRetrievalResult(
            hits=(
                MemoryProviderHit(
                    text="Alice owns Atlas launch communications.",
                    score=0.99,
                    provider_name="graph-memory",
                    bucket="collaborator",
                ),
            ),
            summary="Provider-backed user model available.",
        )

    adapter.retrieve = crowded_retrieve
    adapter.augment_model = high_value_model
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
                    context="- [project] Atlas launch",
                    buckets={"project": ("Atlas launch",)},
                    degraded=False,
                    hits=(),
                ),
            ),
        ):
            retrieval = await plan_memory_retrieval(query="atlas launch", active_projects=("Atlas launch",))
    finally:
        clear_memory_provider_adapters()

    assert retrieval.lane == "hybrid_plus_provider_model"
    assert "Alice owns Atlas launch communications." in retrieval.semantic_context
    assert retrieval.provider_diagnostics[0]["capabilities_used"] == ["retrieval", "user_model"]


@pytest.mark.asyncio
async def test_plan_memory_retrieval_suppresses_stale_provider_retrieval_hits(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _write_memory_provider_extension(workspace, capabilities=("retrieval",))
    stale_created_at = datetime.now(timezone.utc) - timedelta(days=220)
    adapter = FakeMemoryProviderAdapter(capabilities=("retrieval",))

    async def stale_only_retrieve(*, query: str, active_projects: tuple[str, ...] = (), limit: int = 4, config=None):
        return MemoryProviderRetrievalResult(
            hits=(
                MemoryProviderHit(
                    text="Atlas launch status is unchanged from last year.",
                    score=0.62,
                    provider_name="graph-memory",
                    bucket="project",
                    created_at=stale_created_at,
                ),
            ),
            summary="Provider-assisted retrieval available.",
        )

    adapter.retrieve = stale_only_retrieve
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
                    context="- [project] Atlas launch",
                    buckets={"project": ("Atlas launch",)},
                    degraded=False,
                    hits=(),
                ),
            ),
        ):
            retrieval = await plan_memory_retrieval(query="atlas launch status", active_projects=("Atlas launch",))
    finally:
        clear_memory_provider_adapters()

    assert retrieval.lane == "hybrid"
    assert "Atlas launch status is unchanged from last year." not in retrieval.semantic_context
    assert retrieval.provider_diagnostics[0]["attempted_capabilities"] == ["retrieval"]
    assert retrieval.provider_diagnostics[0]["capabilities_used"] == []
    assert retrieval.provider_diagnostics[0]["stale_hit_count"] == 1
    assert retrieval.provider_diagnostics[0]["stale_bucket_counts"]["project"] == 1


@pytest.mark.asyncio
async def test_plan_memory_retrieval_suppresses_stale_provider_user_model_hits(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _write_memory_provider_extension(workspace, capabilities=("user_model",))
    stale_created_at = datetime.now(timezone.utc) - timedelta(days=180)
    fresh_created_at = datetime.now(timezone.utc) - timedelta(days=4)
    adapter = FakeMemoryProviderAdapter(
        capabilities=("user_model",),
        model_hits=(
            MemoryProviderHit(
                text="Atlas launch remains the live project anchor.",
                score=0.71,
                provider_name="graph-memory",
                bucket="project",
                created_at=fresh_created_at,
            ),
            MemoryProviderHit(
                text="Alice owns Atlas launch communications.",
                score=0.83,
                provider_name="graph-memory",
                bucket="collaborator",
                created_at=stale_created_at,
            ),
        ),
    )
    register_memory_provider_adapter(adapter)
    try:
        with (
            patch.object(settings, "workspace_dir", str(workspace)),
            patch(
                "src.memory.retrieval_planner.build_structured_memory_context_bundle",
                return_value=("- [goal] Keep Atlas moving", {"goal": ("Keep Atlas moving",)}),
            ),
        ):
            retrieval = await plan_memory_retrieval(query="", active_projects=("Atlas launch",))
    finally:
        clear_memory_provider_adapters()

    assert retrieval.lane == "structured_plus_provider_model"
    assert "Atlas launch remains the live project anchor." in retrieval.semantic_context
    assert "Alice owns Atlas launch communications." not in retrieval.semantic_context
    assert retrieval.provider_diagnostics[0]["attempted_capabilities"] == ["user_model"]
    assert retrieval.provider_diagnostics[0]["capabilities_used"] == ["user_model"]
    assert retrieval.provider_diagnostics[0]["stale_hit_count"] == 1
    assert retrieval.provider_diagnostics[0]["stale_bucket_counts"]["collaborator"] == 1


@pytest.mark.asyncio
async def test_plan_memory_retrieval_suppresses_irrelevant_project_scoped_provider_hits(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _write_memory_provider_extension(workspace, capabilities=("user_model",))
    adapter = FakeMemoryProviderAdapter(
        capabilities=("user_model",),
        model_hits=(
            MemoryProviderHit(
                text="Hermes migration remains the live project anchor.",
                score=0.91,
                provider_name="graph-memory",
                bucket="project",
                created_at=datetime.now(timezone.utc) - timedelta(days=1),
            ),
            MemoryProviderHit(
                text="Alice owns Atlas launch communications.",
                score=0.67,
                provider_name="graph-memory",
                bucket="collaborator",
                created_at=datetime.now(timezone.utc) - timedelta(days=1),
            ),
        ),
    )
    register_memory_provider_adapter(adapter)
    try:
        with (
            patch.object(settings, "workspace_dir", str(workspace)),
            patch(
                "src.memory.retrieval_planner.build_structured_memory_context_bundle",
                return_value=("- [goal] Keep Atlas moving", {"goal": ("Keep Atlas moving",)}),
            ),
        ):
            retrieval = await plan_memory_retrieval(query="", active_projects=("Atlas launch",))
    finally:
        clear_memory_provider_adapters()

    diagnostics = retrieval.provider_diagnostics[0]
    assert "Hermes migration remains the live project anchor." not in retrieval.semantic_context
    assert "Alice owns Atlas launch communications." in retrieval.semantic_context
    assert diagnostics["capabilities_used"] == ["user_model"]
    assert diagnostics["suppressed_irrelevant_hit_count"] == 1
    assert diagnostics["suppressed_irrelevant_bucket_counts"]["project"] == 1
    assert diagnostics["quality_state"] == "guarded"
    assert diagnostics["topic_matches"] == ["Atlas launch"]


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
    assert retrieval.provider_diagnostics[0]["failed_capabilities"] == ["retrieval"]


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


@pytest.mark.asyncio
async def test_memory_provider_writeback_runs_after_canonical_memory_is_persisted(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _write_memory_provider_extension(workspace, capabilities=("consolidation",))
    adapter = FakeMemoryProviderAdapter(capabilities=("consolidation",))
    register_memory_provider_adapter(adapter)
    try:
        with patch.object(settings, "workspace_dir", str(workspace)):
            result = await writeback_additive_memory_providers(
                memories=(
                    ConsolidatedMemoryItem(
                        text="Atlas launch is the active release project.",
                        kind=normalize_memory_kind("project"),
                        category=kind_to_category("project"),
                        summary="Atlas launch",
                    ),
                    ConsolidatedMemoryItem(
                        text="Send the investor brief before Friday.",
                        kind=normalize_memory_kind("commitment"),
                        category=kind_to_category("commitment"),
                        summary="Send investor brief before Friday",
                    ),
                ),
                session_id="s1",
                trigger="workflow_completed",
                workflow_name="atlas-launch",
            )
    finally:
        clear_memory_provider_adapters()

    assert result.partial_write_count == 0
    assert result.write_failure_count == 0
    assert result.diagnostics[0]["name"] == "graph-memory"
    assert result.diagnostics[0]["canonical_authority"] == "guardian"
    assert result.diagnostics[0]["provenance"] == "provider_mirror"
    assert result.diagnostics[0]["sync_policy"] == "post_canonical_guarded_writeback"
    assert result.diagnostics[0]["capability_contracts_used"]["consolidation"]["requires_canonical_persistence"] is True
    assert result.diagnostics[0]["runtime_state"] == "ready"
    assert result.diagnostics[0]["stored_count"] == 2
    assert result.diagnostics[0]["capabilities_used"] == ["consolidation"]
    assert set(result.diagnostics[0]["accepted_kinds"]) == {"commitment", "project"}
    assert adapter.writeback_calls[0]["session_id"] == "s1"
    assert adapter.writeback_calls[0]["trigger"] == "workflow_completed"


@pytest.mark.asyncio
async def test_memory_provider_writeback_degrades_cleanly_when_provider_fails(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _write_memory_provider_extension(workspace, capabilities=("consolidation",))
    adapter = FakeMemoryProviderAdapter(capabilities=("consolidation",), writeback_should_fail=True)
    register_memory_provider_adapter(adapter)
    try:
        with patch.object(settings, "workspace_dir", str(workspace)):
            result = await writeback_additive_memory_providers(
                memories=(
                    ConsolidatedMemoryItem(
                        text="Atlas launch is the active release project.",
                        kind=normalize_memory_kind("project"),
                        category=kind_to_category("project"),
                        summary="Atlas launch",
                    ),
                ),
                session_id="s1",
                trigger="post_response",
            )
    finally:
        clear_memory_provider_adapters()

    assert result.partial_write_count == 1
    assert result.write_failure_count == 1
    assert result.diagnostics[0]["runtime_state"] == "unavailable"
    assert result.diagnostics[0]["failed_capabilities"] == ["consolidation"]


@pytest.mark.asyncio
async def test_memory_provider_writeback_filters_low_quality_and_duplicate_memories(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _write_memory_provider_extension(workspace, capabilities=("consolidation",))
    adapter = FakeMemoryProviderAdapter(capabilities=("consolidation",))
    register_memory_provider_adapter(adapter)
    try:
        with patch.object(settings, "workspace_dir", str(workspace)):
            result = await writeback_additive_memory_providers(
                memories=(
                    ConsolidatedMemoryItem(
                        text="Atlas launch is the active release project.",
                        kind=normalize_memory_kind("project"),
                        category=kind_to_category("project"),
                        summary="Atlas launch",
                        confidence=0.91,
                        importance=0.9,
                    ),
                    ConsolidatedMemoryItem(
                        text="Atlas launch is the active release project.",
                        kind=normalize_memory_kind("project"),
                        category=kind_to_category("project"),
                        summary="Atlas launch",
                        confidence=0.91,
                        importance=0.9,
                    ),
                    ConsolidatedMemoryItem(
                        text="Maybe think about Atlas sometime.",
                        kind=normalize_memory_kind("commitment"),
                        category=kind_to_category("commitment"),
                        summary="Maybe think about Atlas sometime",
                        confidence=0.21,
                        importance=0.32,
                    ),
                ),
                session_id="s1",
                trigger="workflow_completed",
                workflow_name="atlas-launch",
            )
    finally:
        clear_memory_provider_adapters()

    diagnostics = result.diagnostics[0]
    assert adapter.writeback_calls[0]["kinds"] == ["project"]
    assert diagnostics["eligible_memory_count"] == 1
    assert diagnostics["suppressed_memory_count"] == 2
    assert diagnostics["suppressed_reason_counts"]["duplicate"] == 1
    assert diagnostics["suppressed_reason_counts"]["low_quality"] == 1
    assert diagnostics["suppressed_kind_counts"]["project"] == 1
    assert diagnostics["suppressed_kind_counts"]["commitment"] == 1


@pytest.mark.asyncio
async def test_memory_provider_writeback_suppresses_project_scoped_memory_without_project_anchor(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _write_memory_provider_extension(workspace, capabilities=("consolidation",))
    adapter = FakeMemoryProviderAdapter(capabilities=("consolidation",))
    register_memory_provider_adapter(adapter)
    try:
        with patch.object(settings, "workspace_dir", str(workspace)):
            result = await writeback_additive_memory_providers(
                memories=(
                    ConsolidatedMemoryItem(
                        text="Alice owns launch communications.",
                        kind=normalize_memory_kind("collaborator"),
                        category=kind_to_category("collaborator"),
                        summary="Alice owns launch communications",
                        subject_name="Alice",
                        confidence=0.91,
                        importance=0.9,
                    ),
                    ConsolidatedMemoryItem(
                        text="Alice owns Atlas launch communications.",
                        kind=normalize_memory_kind("collaborator"),
                        category=kind_to_category("collaborator"),
                        summary="Alice owns Atlas launch communications",
                        subject_name="Alice",
                        project_name="Atlas launch",
                        confidence=0.92,
                        importance=0.9,
                    ),
                ),
                session_id="s1",
                trigger="workflow_completed",
            )
    finally:
        clear_memory_provider_adapters()

    diagnostics = result.diagnostics[0]
    assert diagnostics["stored_count"] == 1
    assert diagnostics["eligible_memory_count"] == 1
    assert diagnostics["suppressed_reason_counts"]["missing_project_anchor"] == 1
    assert diagnostics["suppressed_kind_counts"]["collaborator"] == 1
    assert adapter.writeback_calls[0]["texts"] == ["Alice owns Atlas launch communications."]


@pytest.mark.asyncio
async def test_memory_provider_writeback_keeps_high_quality_duplicate_when_low_quality_copy_appears_first(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _write_memory_provider_extension(workspace, capabilities=("consolidation",))
    adapter = FakeMemoryProviderAdapter(capabilities=("consolidation",))
    register_memory_provider_adapter(adapter)
    try:
        with patch.object(settings, "workspace_dir", str(workspace)):
            result = await writeback_additive_memory_providers(
                memories=(
                    ConsolidatedMemoryItem(
                        text="Atlas launch is the active release project.",
                        kind=normalize_memory_kind("project"),
                        category=kind_to_category("project"),
                        summary="Atlas launch",
                        confidence=0.21,
                        importance=0.32,
                    ),
                    ConsolidatedMemoryItem(
                        text="Atlas launch is the active release project.",
                        kind=normalize_memory_kind("project"),
                        category=kind_to_category("project"),
                        summary="Atlas launch",
                        confidence=0.91,
                        importance=0.9,
                    ),
                ),
                session_id="s1",
                trigger="workflow_completed",
            )
    finally:
        clear_memory_provider_adapters()

    diagnostics = result.diagnostics[0]
    assert diagnostics["stored_count"] == 1
    assert diagnostics["eligible_memory_count"] == 1
    assert diagnostics["suppressed_reason_counts"]["low_quality"] == 1
    assert diagnostics["suppressed_reason_counts"]["duplicate"] == 0
    assert adapter.writeback_calls[0]["texts"] == ["Atlas launch is the active release project."]
