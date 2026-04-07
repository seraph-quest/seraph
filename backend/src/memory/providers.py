from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol

from config.settings import settings
from src.db.models import MemoryKind
from src.extensions.registry import ExtensionRegistry, default_manifest_roots_for_workspace
from src.extensions.state import connector_enabled_overrides, load_extension_state_payload
from src.memory.types import ConsolidatedMemoryItem, normalize_memory_kind

logger = logging.getLogger(__name__)

_SUPPORTED_MEMORY_PROVIDER_CAPABILITIES = ("retrieval", "user_model", "consolidation")
_MODELING_BUCKETS = (
    "goal",
    "commitment",
    "preference",
    "pattern",
    "project",
    "collaborator",
    "obligation",
    "routine",
    "timeline",
)

_PROVIDER_STALE_WINDOWS_DAYS = {
    MemoryKind.commitment: 30,
    MemoryKind.project: 45,
    MemoryKind.timeline: 30,
    MemoryKind.preference: 120,
    MemoryKind.communication_preference: 120,
    MemoryKind.procedural: 45,
    MemoryKind.collaborator: 120,
    MemoryKind.obligation: 60,
    MemoryKind.routine: 90,
    MemoryKind.goal: 180,
    MemoryKind.pattern: 180,
    MemoryKind.reflection: 180,
    MemoryKind.fact: 180,
}


@dataclass(frozen=True)
class MemoryProviderHit:
    text: str
    score: float
    provider_name: str
    bucket: str = "external_memory"
    source: str = "memory_provider"
    created_at: datetime | None = None


@dataclass(frozen=True)
class MemoryProviderRetrievalResult:
    hits: tuple[MemoryProviderHit, ...] = ()
    degraded: bool = False
    summary: str = ""
    notes: tuple[str, ...] = ()


class MemoryProviderAdapter(Protocol):
    name: str
    provider_kind: str
    capabilities: tuple[str, ...]

    def health(self) -> dict[str, Any]:
        ...

    async def retrieve(
        self,
        *,
        query: str,
        active_projects: tuple[str, ...] = (),
        limit: int = 4,
        config: dict[str, Any] | None = None,
    ) -> MemoryProviderRetrievalResult:
        ...

    async def writeback(
        self,
        *,
        memories: tuple[ConsolidatedMemoryItem, ...],
        session_id: str,
        trigger: str,
        workflow_name: str | None = None,
        config: dict[str, Any] | None = None,
    ) -> "MemoryProviderWritebackResult":
        ...


@dataclass(frozen=True)
class MemoryProviderInventoryItem:
    name: str
    provider_kind: str
    description: str
    enabled: bool
    configured: bool
    runtime_state: str
    capabilities: tuple[str, ...]
    canonical_memory_owner: str
    canonical_write_mode: str
    extension_id: str | None = None
    reference: str | None = None
    notes: tuple[str, ...] = ()

    def as_payload(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "provider_kind": self.provider_kind,
            "description": self.description,
            "enabled": self.enabled,
            "configured": self.configured,
            "runtime_state": self.runtime_state,
            "capabilities": list(self.capabilities),
            "canonical_memory_owner": self.canonical_memory_owner,
            "canonical_write_mode": self.canonical_write_mode,
            "extension_id": self.extension_id,
            "reference": self.reference,
            "notes": list(self.notes),
        }


@dataclass(frozen=True)
class MemoryProviderAggregateResult:
    context: str
    buckets: dict[str, tuple[str, ...]]
    degraded: bool
    diagnostics: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class MemoryProviderWritebackResult:
    stored_count: int = 0
    partial_write_count: int = 0
    write_failure_count: int = 0
    degraded: bool = False
    summary: str = ""
    notes: tuple[str, ...] = ()
    accepted_kinds: tuple[str, ...] = ()


@dataclass(frozen=True)
class MemoryProviderWritebackAggregateResult:
    diagnostics: tuple[dict[str, Any], ...] = ()
    partial_write_count: int = 0
    write_failure_count: int = 0


_REGISTERED_MEMORY_PROVIDER_ADAPTERS: dict[str, MemoryProviderAdapter] = {}


def register_memory_provider_adapter(adapter: MemoryProviderAdapter) -> None:
    _REGISTERED_MEMORY_PROVIDER_ADAPTERS[str(adapter.name)] = adapter


def unregister_memory_provider_adapter(name: str) -> None:
    _REGISTERED_MEMORY_PROVIDER_ADAPTERS.pop(name, None)


def clear_memory_provider_adapters() -> None:
    _REGISTERED_MEMORY_PROVIDER_ADAPTERS.clear()


def get_memory_provider_adapter(name: str) -> MemoryProviderAdapter | None:
    return _REGISTERED_MEMORY_PROVIDER_ADAPTERS.get(name)


def _capability_handler_available(adapter: MemoryProviderAdapter | None, capability: str) -> bool:
    if adapter is None:
        return False
    if capability == "retrieval":
        return callable(getattr(adapter, "retrieve", None))
    if capability == "user_model":
        return callable(getattr(adapter, "augment_model", None))
    if capability == "consolidation":
        return callable(getattr(adapter, "writeback", None))
    return False


def _capability_runtime_state(
    *,
    capability: str,
    declared_capabilities: tuple[str, ...],
    base_runtime_state: str,
    adapter: MemoryProviderAdapter | None,
) -> str:
    if capability not in declared_capabilities:
        return "undeclared"
    if base_runtime_state in {"disabled", "requires_config", "no_adapter", "unavailable"}:
        return base_runtime_state
    if not _capability_handler_available(adapter, capability):
        return "unsupported"
    return base_runtime_state


def _merge_provider_notes(existing: list[str], additions: tuple[str, ...] | list[str]) -> list[str]:
    for note in additions:
        text = str(note or "").strip()
        if text and text not in existing:
            existing.append(text)
    return existing


def _normalize_modeling_hits(hits: tuple[MemoryProviderHit, ...], *, provider_name: str) -> tuple[MemoryProviderHit, ...]:
    normalized: list[MemoryProviderHit] = []
    for hit in hits:
        bucket = str(hit.bucket or "").strip()
        if bucket not in _MODELING_BUCKETS:
            logger.debug(
                "Skipping unsupported provider modeling bucket",
                extra={"provider_name": provider_name, "bucket": bucket},
            )
            continue
        normalized.append(hit)
    return tuple(normalized)


def _normalize_provider_timestamp(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _provider_hit_is_stale(hit: MemoryProviderHit, *, now: datetime) -> bool:
    created_at = _normalize_provider_timestamp(hit.created_at)
    if created_at is None:
        return False
    try:
        bucket_kind = normalize_memory_kind(hit.bucket)
    except ValueError:
        return False
    window_days = _PROVIDER_STALE_WINDOWS_DAYS.get(bucket_kind)
    if window_days is None:
        return False
    age_days = max(0.0, (now - created_at).total_seconds() / 86_400)
    return age_days > float(window_days)


def _filter_stale_provider_hits(
    hits: tuple[MemoryProviderHit, ...] | list[MemoryProviderHit],
    *,
    now: datetime,
) -> tuple[tuple[MemoryProviderHit, ...], dict[str, int]]:
    fresh: list[MemoryProviderHit] = []
    stale_bucket_counts: dict[str, int] = {}
    for hit in hits:
        if _provider_hit_is_stale(hit, now=now):
            stale_bucket_counts[hit.bucket] = stale_bucket_counts.get(hit.bucket, 0) + 1
            continue
        fresh.append(hit)
    return tuple(fresh), stale_bucket_counts


def _required_config_missing(config_fields: list[dict[str, Any]], config_entry: dict[str, Any]) -> bool:
    for field in config_fields:
        if not isinstance(field, dict):
            continue
        key = field.get("key")
        if not isinstance(key, str) or not key:
            continue
        if not bool(field.get("required", False)):
            continue
        value = config_entry.get(key)
        if value is None:
            return True
        if isinstance(value, str) and not value.strip():
            return True
    return False


def _memory_provider_config(
    state_by_id: dict[str, Any] | None,
    extension_id: str,
    provider_name: str,
) -> dict[str, Any]:
    if not isinstance(state_by_id, dict):
        return {}
    raw_state = state_by_id.get(extension_id)
    if not isinstance(raw_state, dict):
        return {}
    raw_config = raw_state.get("config")
    if not isinstance(raw_config, dict):
        return {}
    bucket = raw_config.get("memory_providers")
    if not isinstance(bucket, dict):
        return {}
    entry = bucket.get(provider_name)
    return entry if isinstance(entry, dict) else {}


def list_memory_provider_inventory() -> dict[str, Any]:
    state_payload = load_extension_state_payload()
    state_by_id = state_payload.get("extensions")
    if not isinstance(state_by_id, dict):
        state_by_id = {}
    enabled_overrides = connector_enabled_overrides(state_by_id)
    snapshot = ExtensionRegistry(
        manifest_roots=default_manifest_roots_for_workspace(settings.workspace_dir),
        skill_dirs=[],
        workflow_dirs=[],
        mcp_runtime=None,
    ).snapshot()

    providers: list[MemoryProviderInventoryItem] = []
    for contribution in snapshot.list_contributions("memory_providers"):
        if isinstance(contribution.metadata.get("registry_conflict"), dict):
            continue
        name = contribution.metadata.get("name")
        provider_kind = contribution.metadata.get("provider_kind")
        if not isinstance(name, str) or not name.strip():
            continue
        if not isinstance(provider_kind, str) or not provider_kind.strip():
            continue
        default_enabled = bool(contribution.metadata.get("default_enabled", False))
        enabled = (
            enabled_overrides.get((contribution.extension_id, contribution.reference), default_enabled)
            if enabled_overrides
            else default_enabled
        )
        config_fields = contribution.metadata.get("config_fields")
        config_field_list = config_fields if isinstance(config_fields, list) else []
        config_entry = _memory_provider_config(state_by_id, contribution.extension_id, name)
        configured = not _required_config_missing(config_field_list, config_entry)
        adapter = get_memory_provider_adapter(name)
        declared_capabilities = tuple(
            item
            for item in contribution.metadata.get("capabilities", [])
            if isinstance(item, str) and item.strip()
        ) if isinstance(contribution.metadata.get("capabilities"), list) else ()
        notes: list[str] = [
            "Canonical guardian memory remains authoritative; external providers are additive only.",
        ]
        if not enabled:
            runtime_state = "disabled"
        elif not configured:
            runtime_state = "requires_config"
            notes.append("Configure the required provider fields before additive retrieval can run.")
        elif adapter is None:
            runtime_state = "no_adapter"
            notes.append("No runtime adapter is registered for this configured provider.")
        else:
            try:
                health = adapter.health() if callable(getattr(adapter, "health", None)) else {}
            except Exception:
                logger.debug("Memory provider health check failed", exc_info=True)
                runtime_state = "unavailable"
                notes.append("Provider health check failed; guardian memory remains authoritative.")
            else:
                status = str(health.get("status") or "ready")
                runtime_state = status if status in {"ready", "degraded", "unavailable"} else "ready"
                summary = str(health.get("summary") or "").strip()
                if summary:
                    notes.append(summary)
        capability_states = {
            capability: _capability_runtime_state(
                capability=capability,
                declared_capabilities=declared_capabilities,
                base_runtime_state=runtime_state,
                adapter=adapter,
            )
            for capability in _SUPPORTED_MEMORY_PROVIDER_CAPABILITIES
            if capability in declared_capabilities
        }
        if runtime_state in {"ready", "degraded"}:
            unsupported_capabilities = [
                capability
                for capability, state in capability_states.items()
                if state == "unsupported"
            ]
            if unsupported_capabilities:
                notes.append(
                    "Declared provider capabilities without a runtime handler stay visible but cannot run yet: "
                    + ", ".join(sorted(unsupported_capabilities))
                    + "."
                )
        providers.append(
            MemoryProviderInventoryItem(
                name=name,
                provider_kind=provider_kind,
                description=str(contribution.metadata.get("description") or ""),
                enabled=enabled,
                configured=configured,
                runtime_state=runtime_state,
                capabilities=declared_capabilities,
                canonical_memory_owner=str(contribution.metadata.get("canonical_memory_owner") or "seraph"),
                canonical_write_mode=str(contribution.metadata.get("canonical_write_mode") or "additive_only"),
                extension_id=contribution.extension_id,
                reference=contribution.reference,
                notes=tuple(notes + ([f"Capability states: {capability_states}."] if capability_states else [])),
            )
        )

    capability_summary = {
        f"{capability}_ready_count": 0
        for capability in _SUPPORTED_MEMORY_PROVIDER_CAPABILITIES
    }
    capability_summary.update(
        {
            f"{capability}_degraded_count": 0
            for capability in _SUPPORTED_MEMORY_PROVIDER_CAPABILITIES
        }
    )
    provider_payloads = [item.as_payload() for item in providers]
    for item in provider_payloads:
        capability_states = item.setdefault("capability_states", {})
        capability_states.update(
            {
                capability: _capability_runtime_state(
                    capability=capability,
                    declared_capabilities=tuple(item.get("capabilities", [])),
                    base_runtime_state=str(item.get("runtime_state") or "unavailable"),
                    adapter=get_memory_provider_adapter(str(item.get("name") or "")),
                )
                for capability in item.get("capabilities", [])
                if capability in _SUPPORTED_MEMORY_PROVIDER_CAPABILITIES
            }
        )
        item["governance"] = {
            "authoritative_memory": "guardian",
            "augmentation_mode": "additive_only",
            "modeled_buckets": list(_MODELING_BUCKETS),
            "writeback_state": capability_states.get("consolidation", "undeclared"),
        }
        for capability, state in capability_states.items():
            if state == "ready":
                capability_summary[f"{capability}_ready_count"] += 1
            elif state == "degraded":
                capability_summary[f"{capability}_degraded_count"] += 1

    return {
        "providers": provider_payloads,
        "summary": {
            "provider_count": len(providers),
            "ready_count": sum(1 for item in providers if item.runtime_state == "ready"),
            "degraded_count": sum(1 for item in providers if item.runtime_state == "degraded"),
            "configured_count": sum(1 for item in providers if item.configured),
            **capability_summary,
        },
        "composition_rules": [
            "Canonical guardian memory stays authoritative; external providers only augment retrieval or modeling.",
            "Configured providers without a registered runtime adapter remain visible but cannot participate in retrieval.",
            "Provider failures must degrade cleanly back to canonical guardian memory instead of blocking retrieval.",
        ],
        "governance_rules": [
            "Provider-backed user or project modeling is additive only; it must not silently replace canonical guardian memory.",
            "Provider-backed consolidation or writeback runs only after canonical guardian persistence succeeds and remains advisory.",
            "When canonical and provider context conflict, guardian-owned memory remains authoritative and provider evidence is advisory.",
        ],
    }


async def retrieve_additive_memory_provider_context(
    *,
    query: str,
    active_projects: tuple[str, ...] = (),
    limit: int = 4,
    include_user_model: bool = False,
) -> MemoryProviderAggregateResult:
    inventory = list_memory_provider_inventory()
    items = inventory.get("providers", [])
    all_hits: list[MemoryProviderHit] = []
    diagnostics: list[dict[str, Any]] = []
    degraded = False
    now = datetime.now(timezone.utc)

    for item in items:
        if not isinstance(item, dict):
            continue
        if not bool(item.get("enabled")) or not bool(item.get("configured")):
            continue
        if str(item.get("runtime_state") or "") not in {"ready", "degraded"}:
            continue
        name = str(item.get("name") or "")
        adapter = get_memory_provider_adapter(name)
        if adapter is None:
            continue
        capability_states = item.get("capability_states")
        if not isinstance(capability_states, dict):
            capability_states = {}
        config = _memory_provider_config(
            load_extension_state_payload().get("extensions")
            if isinstance(load_extension_state_payload().get("extensions"), dict)
            else {},
            str(item.get("extension_id") or ""),
            name,
        )
        provider_hits: list[MemoryProviderHit] = []
        provider_notes: list[str] = []
        provider_summaries: list[str] = []
        attempted_capabilities: list[str] = []
        capabilities_used: list[str] = []
        failed_capabilities: list[str] = []
        provider_degraded = False
        stale_bucket_counts: dict[str, int] = {}

        retrieval_state = str(capability_states.get("retrieval") or "")
        if query.strip() and retrieval_state in {"ready", "degraded"} and "retrieval" in item.get("capabilities", []):
            attempted_capabilities.append("retrieval")
            try:
                result = await adapter.retrieve(
                    query=query,
                    active_projects=active_projects,
                    limit=limit,
                    config=config,
                )
            except Exception:
                logger.debug("Memory provider retrieval failed", exc_info=True)
                failed_capabilities.append("retrieval")
                provider_notes.append("Provider retrieval failed; canonical guardian memory remained in control.")
            else:
                fresh_hits, stale_counts = _filter_stale_provider_hits(result.hits[:limit], now=now)
                for bucket, count in stale_counts.items():
                    stale_bucket_counts[bucket] = stale_bucket_counts.get(bucket, 0) + count
                provider_hits.extend(fresh_hits)
                provider_summaries.append(result.summary)
                _merge_provider_notes(provider_notes, result.notes)
                if fresh_hits:
                    capabilities_used.append("retrieval")
                provider_degraded = provider_degraded or result.degraded or retrieval_state == "degraded"

        user_model_state = str(capability_states.get("user_model") or "")
        augment_model = getattr(adapter, "augment_model", None)
        if (
            include_user_model
            and active_projects
            and user_model_state in {"ready", "degraded"}
            and "user_model" in item.get("capabilities", [])
            and callable(augment_model)
        ):
            attempted_capabilities.append("user_model")
            try:
                result = await augment_model(
                    active_projects=active_projects,
                    limit=limit,
                    config=config,
                )
            except Exception:
                logger.debug("Memory provider user-model augmentation failed", exc_info=True)
                failed_capabilities.append("user_model")
                provider_notes.append("Provider user-model augmentation failed; canonical guardian memory remained in control.")
            else:
                normalized_hits = _normalize_modeling_hits(result.hits[:limit], provider_name=name)
                fresh_hits, stale_counts = _filter_stale_provider_hits(normalized_hits, now=now)
                for bucket, count in stale_counts.items():
                    stale_bucket_counts[bucket] = stale_bucket_counts.get(bucket, 0) + count
                provider_hits.extend(fresh_hits)
                provider_summaries.append(result.summary)
                _merge_provider_notes(provider_notes, result.notes)
                if fresh_hits:
                    capabilities_used.append("user_model")
                provider_degraded = provider_degraded or result.degraded or user_model_state == "degraded"

        stale_hit_count = sum(stale_bucket_counts.values())
        if stale_hit_count:
            provider_notes.append(
                "Stale provider evidence was suppressed so canonical guardian memory remained the active source of truth."
            )

        if not attempted_capabilities and not failed_capabilities:
            continue

        all_hits.extend(provider_hits[:limit])
        bucket_counts: dict[str, int] = {}
        for hit in provider_hits:
            bucket_counts[hit.bucket] = bucket_counts.get(hit.bucket, 0) + 1
        diagnostics.append(
            {
                "name": name,
                "runtime_state": (
                    "unavailable"
                    if failed_capabilities and not capabilities_used
                    else "degraded" if provider_degraded or failed_capabilities else "ready"
                ),
                "hit_count": len(provider_hits),
                "degraded": provider_degraded,
                "summary": " ".join(summary for summary in provider_summaries if summary).strip(),
                "notes": provider_notes,
                "attempted_capabilities": attempted_capabilities,
                "capabilities_used": capabilities_used,
                "failed_capabilities": failed_capabilities,
                "bucket_counts": bucket_counts,
                "stale_hit_count": stale_hit_count,
                "stale_bucket_counts": stale_bucket_counts,
            }
        )

    seen_lines: set[str] = set()
    bucket_values: dict[str, list[str]] = {}
    lines: list[str] = []
    for hit in sorted(all_hits, key=lambda item: (-item.score, item.provider_name, item.text.lower()))[:limit]:
        bucket_values.setdefault(hit.bucket, [])
        if hit.text not in bucket_values[hit.bucket]:
            bucket_values[hit.bucket].append(hit.text)
        line = f"- [{hit.bucket}] {hit.provider_name}: {hit.text}"
        if line in seen_lines:
            continue
        seen_lines.add(line)
        lines.append(line)

    return MemoryProviderAggregateResult(
        context="\n".join(lines),
        buckets={key: tuple(values) for key, values in bucket_values.items()},
        degraded=degraded,
        diagnostics=tuple(diagnostics),
    )


async def writeback_additive_memory_providers(
    *,
    memories: tuple[ConsolidatedMemoryItem, ...],
    session_id: str,
    trigger: str,
    workflow_name: str | None = None,
) -> MemoryProviderWritebackAggregateResult:
    if not memories:
        return MemoryProviderWritebackAggregateResult()

    inventory = list_memory_provider_inventory()
    items = inventory.get("providers", [])
    diagnostics: list[dict[str, Any]] = []
    partial_write_count = 0
    write_failure_count = 0

    for item in items:
        if not isinstance(item, dict):
            continue
        if not bool(item.get("enabled")) or not bool(item.get("configured")):
            continue
        if str(item.get("runtime_state") or "") not in {"ready", "degraded"}:
            continue
        if "consolidation" not in item.get("capabilities", []):
            continue
        capability_states = item.get("capability_states")
        if not isinstance(capability_states, dict):
            capability_states = {}
        consolidation_state = str(capability_states.get("consolidation") or "")
        if consolidation_state not in {"ready", "degraded"}:
            continue

        name = str(item.get("name") or "")
        adapter = get_memory_provider_adapter(name)
        writeback = getattr(adapter, "writeback", None) if adapter is not None else None
        if not callable(writeback):
            continue

        config = _memory_provider_config(
            load_extension_state_payload().get("extensions")
            if isinstance(load_extension_state_payload().get("extensions"), dict)
            else {},
            str(item.get("extension_id") or ""),
            name,
        )
        provider_notes: list[str] = []
        try:
            result = await writeback(
                memories=memories,
                session_id=session_id,
                trigger=trigger,
                workflow_name=workflow_name,
                config=config,
            )
        except Exception:
            logger.debug("Memory provider writeback failed", exc_info=True)
            provider_notes.append(
                "Provider writeback failed after canonical guardian persistence; canonical memory remained authoritative."
            )
            diagnostics.append(
                {
                    "name": name,
                    "runtime_state": "unavailable",
                    "stored_count": 0,
                    "partial_write_count": 1,
                    "write_failure_count": 1,
                    "degraded": True,
                    "summary": "",
                    "notes": provider_notes,
                    "capabilities_used": [],
                    "failed_capabilities": ["consolidation"],
                    "accepted_kinds": [],
                }
            )
            partial_write_count += 1
            write_failure_count += 1
            continue

        _merge_provider_notes(provider_notes, result.notes)
        diagnostics.append(
            {
                "name": name,
                "runtime_state": "degraded" if result.degraded or consolidation_state == "degraded" else "ready",
                "stored_count": int(result.stored_count),
                "partial_write_count": int(result.partial_write_count),
                "write_failure_count": int(result.write_failure_count),
                "degraded": bool(result.degraded),
                "summary": result.summary.strip(),
                "notes": provider_notes,
                "capabilities_used": ["consolidation"],
                "failed_capabilities": ["consolidation"] if result.write_failure_count else [],
                "accepted_kinds": [kind for kind in result.accepted_kinds if kind],
            }
        )
        partial_write_count += int(result.partial_write_count)
        write_failure_count += int(result.write_failure_count)

    return MemoryProviderWritebackAggregateResult(
        diagnostics=tuple(diagnostics),
        partial_write_count=partial_write_count,
        write_failure_count=write_failure_count,
    )
