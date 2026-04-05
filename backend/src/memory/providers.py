from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from config.settings import settings
from src.extensions.registry import ExtensionRegistry, default_manifest_roots_for_workspace
from src.extensions.state import connector_enabled_overrides, load_extension_state_payload

logger = logging.getLogger(__name__)


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


_REGISTERED_MEMORY_PROVIDER_ADAPTERS: dict[str, MemoryProviderAdapter] = {}


def register_memory_provider_adapter(adapter: MemoryProviderAdapter) -> None:
    _REGISTERED_MEMORY_PROVIDER_ADAPTERS[str(adapter.name)] = adapter


def unregister_memory_provider_adapter(name: str) -> None:
    _REGISTERED_MEMORY_PROVIDER_ADAPTERS.pop(name, None)


def clear_memory_provider_adapters() -> None:
    _REGISTERED_MEMORY_PROVIDER_ADAPTERS.clear()


def get_memory_provider_adapter(name: str) -> MemoryProviderAdapter | None:
    return _REGISTERED_MEMORY_PROVIDER_ADAPTERS.get(name)


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
        providers.append(
            MemoryProviderInventoryItem(
                name=name,
                provider_kind=provider_kind,
                description=str(contribution.metadata.get("description") or ""),
                enabled=enabled,
                configured=configured,
                runtime_state=runtime_state,
                capabilities=tuple(
                    item
                    for item in contribution.metadata.get("capabilities", [])
                    if isinstance(item, str) and item.strip()
                ) if isinstance(contribution.metadata.get("capabilities"), list) else (),
                canonical_memory_owner=str(contribution.metadata.get("canonical_memory_owner") or "seraph"),
                canonical_write_mode=str(contribution.metadata.get("canonical_write_mode") or "additive_only"),
                extension_id=contribution.extension_id,
                reference=contribution.reference,
                notes=tuple(notes),
            )
        )

    return {
        "providers": [item.as_payload() for item in providers],
        "summary": {
            "provider_count": len(providers),
            "ready_count": sum(1 for item in providers if item.runtime_state == "ready"),
            "degraded_count": sum(1 for item in providers if item.runtime_state == "degraded"),
            "configured_count": sum(1 for item in providers if item.configured),
        },
        "composition_rules": [
            "Canonical guardian memory stays authoritative; external providers only augment retrieval or modeling.",
            "Configured providers without a registered runtime adapter remain visible but cannot participate in retrieval.",
            "Provider failures must degrade cleanly back to canonical guardian memory instead of blocking retrieval.",
        ],
    }


async def retrieve_additive_memory_provider_context(
    *,
    query: str,
    active_projects: tuple[str, ...] = (),
    limit: int = 4,
) -> MemoryProviderAggregateResult:
    inventory = list_memory_provider_inventory()
    items = inventory.get("providers", [])
    all_hits: list[MemoryProviderHit] = []
    diagnostics: list[dict[str, Any]] = []
    degraded = False

    for item in items:
        if not isinstance(item, dict):
            continue
        if "retrieval" not in item.get("capabilities", []):
            continue
        if not bool(item.get("enabled")) or not bool(item.get("configured")):
            continue
        if str(item.get("runtime_state") or "") not in {"ready", "degraded"}:
            continue
        name = str(item.get("name") or "")
        adapter = get_memory_provider_adapter(name)
        if adapter is None:
            continue
        config = _memory_provider_config(
            load_extension_state_payload().get("extensions")
            if isinstance(load_extension_state_payload().get("extensions"), dict)
            else {},
            str(item.get("extension_id") or ""),
            name,
        )
        try:
            result = await adapter.retrieve(
                query=query,
                active_projects=active_projects,
                limit=limit,
                config=config,
            )
        except Exception:
            logger.debug("Memory provider retrieval failed", exc_info=True)
            diagnostics.append(
                {
                    "name": name,
                    "runtime_state": "unavailable",
                    "hit_count": 0,
                    "degraded": False,
                    "summary": "Provider retrieval failed; canonical guardian memory remained in control.",
                }
            )
            continue
        all_hits.extend(result.hits[:limit])
        diagnostics.append(
            {
                "name": name,
                "runtime_state": "degraded" if result.degraded else "ready",
                "hit_count": len(result.hits),
                "degraded": result.degraded,
                "summary": result.summary,
                "notes": list(result.notes),
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
