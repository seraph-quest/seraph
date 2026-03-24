"""Typed Wave 2 capability contribution helpers for extension packages."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from typing import Any

from src.extensions.connectors import ConnectorDefinitionError, load_connector_payload


def _parse_string_list(payload: Any, *, source: str, field_name: str) -> tuple[str, ...]:
    if payload is None:
        return ()
    if not isinstance(payload, list):
        raise ConnectorDefinitionError(f"{source}: {field_name} must be a list")
    normalized: list[str] = []
    seen: set[str] = set()
    for value in payload:
        if not isinstance(value, str) or not value.strip():
            raise ConnectorDefinitionError(f"{source}: {field_name} entries must be non-empty strings")
        item = value.strip()
        if item not in seen:
            normalized.append(item)
            seen.add(item)
    return tuple(normalized)


def _parse_optional_bool(payload: Any, *, source: str, field_name: str) -> bool | None:
    if payload is None:
        return None
    if not isinstance(payload, bool):
        raise ConnectorDefinitionError(f"{source}: {field_name} must be a boolean")
    return payload


@dataclass(frozen=True)
class ContributionConfigField:
    key: str
    label: str
    required: bool = True
    input: str = "text"

    def as_metadata(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "required": self.required,
            "input": self.input,
        }


def _parse_config_fields(payload: Any, *, source: str, field_name: str = "config_fields") -> tuple[ContributionConfigField, ...]:
    if payload is None:
        return ()
    if not isinstance(payload, list):
        raise ConnectorDefinitionError(f"{source}: {field_name} must be a list")
    fields: list[ContributionConfigField] = []
    seen: set[str] = set()
    for entry in payload:
        if not isinstance(entry, dict):
            raise ConnectorDefinitionError(f"{source}: {field_name} entries must be objects")
        raw_key = entry.get("key")
        raw_label = entry.get("label")
        if not isinstance(raw_key, str) or not raw_key.strip():
            raise ConnectorDefinitionError(f"{source}: {field_name} entry must include a non-empty key")
        if not isinstance(raw_label, str) or not raw_label.strip():
            raise ConnectorDefinitionError(
                f"{source}: {field_name} entry '{raw_key}' must include a non-empty label"
            )
        key = raw_key.strip()
        if key in seen:
            raise ConnectorDefinitionError(f"{source}: duplicate {field_name} key '{key}'")
        seen.add(key)
        raw_required = entry.get("required")
        if raw_required is not None and not isinstance(raw_required, bool):
            raise ConnectorDefinitionError(f"{source}: {field_name} entry '{key}' required must be a boolean")
        raw_input = entry.get("input")
        if raw_input is not None and not isinstance(raw_input, str):
            raise ConnectorDefinitionError(f"{source}: {field_name} entry '{key}' input must be a string")
        input_kind = raw_input.strip() if isinstance(raw_input, str) and raw_input.strip() else "text"
        if input_kind not in {"text", "password", "url", "select"}:
            raise ConnectorDefinitionError(
                f"{source}: {field_name} entry '{key}' input '{input_kind}' is not supported"
            )
        fields.append(
            ContributionConfigField(
                key=key,
                label=raw_label.strip(),
                required=True if raw_required is None else raw_required,
                input=input_kind,
            )
        )
    return tuple(fields)


def _ensure_config_field(
    fields: tuple[ContributionConfigField, ...],
    *,
    key: str,
    label: str,
    input_kind: str,
    required: bool = True,
) -> tuple[ContributionConfigField, ...]:
    if any(field.key == key for field in fields):
        return fields
    return fields + (
        ContributionConfigField(
            key=key,
            label=label,
            required=required,
            input=input_kind,
        ),
    )


@dataclass(frozen=True)
class ToolsetPresetDefinition:
    name: str
    description: str = ""
    mode: str = ""
    include_tools: tuple[str, ...] = ()
    include_mcp_servers: tuple[str, ...] = ()
    exclude_tools: tuple[str, ...] = ()
    capabilities: tuple[str, ...] = ()
    execution_boundaries: tuple[str, ...] = ()
    enabled: bool = True

    def as_metadata(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "mode": self.mode,
            "include_tools": list(self.include_tools),
            "include_mcp_servers": list(self.include_mcp_servers),
            "exclude_tools": list(self.exclude_tools),
            "capabilities": list(self.capabilities),
            "execution_boundaries": list(self.execution_boundaries),
            "default_enabled": self.enabled,
        }


@dataclass(frozen=True)
class ContextPackDefinition:
    name: str
    description: str = ""
    instructions: str = ""
    memory_tags: tuple[str, ...] = ()
    profile_fields: tuple[str, ...] = ()
    prompt_refs: tuple[str, ...] = ()
    domains: tuple[str, ...] = ()

    def as_metadata(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "instructions": self.instructions,
            "memory_tags": list(self.memory_tags),
            "profile_fields": list(self.profile_fields),
            "prompt_refs": list(self.prompt_refs),
            "domains": list(self.domains),
        }


@dataclass(frozen=True)
class PromptPackDefinition:
    name: str
    title: str
    description: str = ""
    instructions: str = ""

    def as_metadata(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "title": self.title,
            "description": self.description,
            "instructions": self.instructions,
        }


@dataclass(frozen=True)
class AutomationTriggerDefinition:
    name: str
    trigger_type: str
    description: str = ""
    enabled: bool = False
    schedule: str = ""
    endpoint: str = ""
    topic: str = ""
    capabilities: tuple[str, ...] = ()
    config_fields: tuple[ContributionConfigField, ...] = ()
    requires_network: bool = False

    def as_metadata(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "trigger_type": self.trigger_type,
            "description": self.description,
            "default_enabled": self.enabled,
            "schedule": self.schedule,
            "endpoint": self.endpoint,
            "topic": self.topic,
            "capabilities": list(self.capabilities),
            "config_fields": [field.as_metadata() for field in self.config_fields],
            "requires_network": self.requires_network,
        }


@dataclass(frozen=True)
class BrowserProviderDefinition:
    name: str
    provider_kind: str
    description: str = ""
    enabled: bool = False
    capabilities: tuple[str, ...] = ()
    config_fields: tuple[ContributionConfigField, ...] = ()
    requires_network: bool = True
    requires_daemon: bool = False

    def as_metadata(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "provider_kind": self.provider_kind,
            "description": self.description,
            "default_enabled": self.enabled,
            "capabilities": list(self.capabilities),
            "config_fields": [field.as_metadata() for field in self.config_fields],
            "requires_network": self.requires_network,
            "requires_daemon": self.requires_daemon,
        }


@dataclass(frozen=True)
class MessagingConnectorDefinition:
    name: str
    platform: str
    description: str = ""
    enabled: bool = False
    delivery_modes: tuple[str, ...] = ()
    config_fields: tuple[ContributionConfigField, ...] = ()
    requires_network: bool = True

    def as_metadata(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "platform": self.platform,
            "description": self.description,
            "default_enabled": self.enabled,
            "delivery_modes": list(self.delivery_modes),
            "config_fields": [field.as_metadata() for field in self.config_fields],
            "requires_network": self.requires_network,
        }


@dataclass(frozen=True)
class SpeechProfileDefinition:
    name: str
    provider: str
    description: str = ""
    voice: str = ""
    supports_tts: bool = False
    supports_stt: bool = False
    wake_word: str = ""

    def as_metadata(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "provider": self.provider,
            "description": self.description,
            "voice": self.voice,
            "supports_tts": self.supports_tts,
            "supports_stt": self.supports_stt,
            "wake_word": self.wake_word,
        }


@dataclass(frozen=True)
class ProviderPresetDefinition:
    name: str
    label: str = ""
    default_model: str = ""
    notes: str = ""

    def as_metadata(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "label": self.label,
            "default_model": self.default_model,
            "notes": self.notes,
        }


@dataclass(frozen=True)
class CanvasOutputDefinition:
    name: str
    title: str
    description: str = ""
    surface_kind: str = "board"
    sections: tuple[str, ...] = ()
    artifact_types: tuple[str, ...] = ()
    preferred_panel: str = ""

    def as_metadata(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "title": self.title,
            "description": self.description,
            "surface_kind": self.surface_kind,
            "sections": list(self.sections),
            "artifact_types": list(self.artifact_types),
            "preferred_panel": self.preferred_panel,
        }


@dataclass(frozen=True)
class WorkflowRuntimeDefinition:
    name: str
    engine_kind: str
    description: str = ""
    delegation_mode: str = ""
    checkpoint_policy: str = ""
    structured_output: bool = False
    default_output_surface: str = ""

    def as_metadata(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "engine_kind": self.engine_kind,
            "description": self.description,
            "delegation_mode": self.delegation_mode,
            "checkpoint_policy": self.checkpoint_policy,
            "structured_output": self.structured_output,
            "default_output_surface": self.default_output_surface,
        }


@dataclass(frozen=True)
class NodeAdapterDefinition:
    name: str
    adapter_kind: str
    description: str = ""
    enabled: bool = False
    capabilities: tuple[str, ...] = ()
    config_fields: tuple[ContributionConfigField, ...] = ()
    requires_network: bool = False
    requires_daemon: bool = True

    def as_metadata(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "adapter_kind": self.adapter_kind,
            "description": self.description,
            "default_enabled": self.enabled,
            "capabilities": list(self.capabilities),
            "config_fields": [field.as_metadata() for field in self.config_fields],
            "requires_network": self.requires_network,
            "requires_daemon": self.requires_daemon,
        }


def _parse_named_object(payload: Any, *, source: str, noun: str) -> tuple[str, str]:
    if not isinstance(payload, dict):
        raise ConnectorDefinitionError(f"{source}: {noun} definition must be an object")
    raw_name = payload.get("name")
    if not isinstance(raw_name, str) or not raw_name.strip():
        raise ConnectorDefinitionError(f"{source}: {noun} definition must include a non-empty name")
    raw_description = payload.get("description")
    if raw_description is not None and not isinstance(raw_description, str):
        raise ConnectorDefinitionError(f"{source}: {noun} description must be a string")
    return raw_name.strip(), raw_description.strip() if isinstance(raw_description, str) else ""


def _slugify_name(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-") or "prompt-pack"


def parse_toolset_preset_definition(payload: Any, *, source: str) -> ToolsetPresetDefinition:
    name, description = _parse_named_object(payload, source=source, noun="toolset preset")
    if not isinstance(payload, dict):
        raise ConnectorDefinitionError(f"{source}: toolset preset definition must be an object")
    raw_mode = payload.get("mode")
    if raw_mode is not None and not isinstance(raw_mode, str):
        raise ConnectorDefinitionError(f"{source}: toolset preset mode must be a string")
    include_tools = _parse_string_list(payload.get("include_tools"), source=source, field_name="include_tools")
    include_mcp_servers = _parse_string_list(
        payload.get("include_mcp_servers"),
        source=source,
        field_name="include_mcp_servers",
    )
    exclude_tools = _parse_string_list(payload.get("exclude_tools"), source=source, field_name="exclude_tools")
    capabilities = _parse_string_list(payload.get("capabilities"), source=source, field_name="capabilities")
    execution_boundaries = _parse_string_list(
        payload.get("execution_boundaries"),
        source=source,
        field_name="execution_boundaries",
    )
    raw_enabled = payload.get("enabled")
    if raw_enabled is not None and not isinstance(raw_enabled, bool):
        raise ConnectorDefinitionError(f"{source}: toolset preset enabled must be a boolean")
    if not any((include_tools, include_mcp_servers, exclude_tools, capabilities, execution_boundaries)):
        raise ConnectorDefinitionError(
            f"{source}: toolset preset must declare tools, MCP servers, capabilities, or execution boundaries"
        )
    return ToolsetPresetDefinition(
        name=name,
        description=description,
        mode=raw_mode.strip() if isinstance(raw_mode, str) and raw_mode.strip() else "",
        include_tools=include_tools,
        include_mcp_servers=include_mcp_servers,
        exclude_tools=exclude_tools,
        capabilities=capabilities,
        execution_boundaries=execution_boundaries,
        enabled=True if raw_enabled is None else raw_enabled,
    )


def parse_context_pack_definition(payload: Any, *, source: str) -> ContextPackDefinition:
    name, description = _parse_named_object(payload, source=source, noun="context pack")
    if not isinstance(payload, dict):
        raise ConnectorDefinitionError(f"{source}: context pack definition must be an object")
    raw_instructions = payload.get("instructions")
    if raw_instructions is not None and not isinstance(raw_instructions, str):
        raise ConnectorDefinitionError(f"{source}: context pack instructions must be a string")
    memory_tags = _parse_string_list(payload.get("memory_tags"), source=source, field_name="memory_tags")
    profile_fields = _parse_string_list(payload.get("profile_fields"), source=source, field_name="profile_fields")
    prompt_refs = _parse_string_list(payload.get("prompt_refs"), source=source, field_name="prompt_refs")
    domains = _parse_string_list(payload.get("domains"), source=source, field_name="domains")
    instructions = raw_instructions.strip() if isinstance(raw_instructions, str) else ""
    if not any((instructions, memory_tags, profile_fields, prompt_refs, domains)):
        raise ConnectorDefinitionError(
            f"{source}: context pack must declare instructions, tags, profile fields, prompt refs, or domains"
        )
    return ContextPackDefinition(
        name=name,
        description=description,
        instructions=instructions,
        memory_tags=memory_tags,
        profile_fields=profile_fields,
        prompt_refs=prompt_refs,
        domains=domains,
    )


def parse_prompt_pack_definition(content: str, *, source: str) -> PromptPackDefinition:
    if not isinstance(content, str):
        raise ConnectorDefinitionError(f"{source}: prompt pack must be UTF-8 text")
    normalized = content.strip()
    if not normalized:
        raise ConnectorDefinitionError(f"{source}: prompt pack must not be empty")

    title = ""
    description = ""
    for raw_line in normalized.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if not title and line.startswith("#"):
            title = line.lstrip("#").strip()
            continue
        if not description and not line.startswith(("-", "*")):
            description = line
            break

    fallback_name = Path(source).stem.replace("_", "-")
    if title:
        name = _slugify_name(title)
    else:
        name = _slugify_name(fallback_name)
        title = fallback_name.replace("-", " ").title()

    return PromptPackDefinition(
        name=name,
        title=title,
        description=description,
        instructions=normalized,
    )


def parse_automation_trigger_definition(payload: Any, *, source: str) -> AutomationTriggerDefinition:
    name, description = _parse_named_object(payload, source=source, noun="automation trigger")
    if not isinstance(payload, dict):
        raise ConnectorDefinitionError(f"{source}: automation trigger definition must be an object")
    raw_trigger_type = payload.get("trigger_type")
    if not isinstance(raw_trigger_type, str) or not raw_trigger_type.strip():
        raise ConnectorDefinitionError(f"{source}: automation trigger must include a non-empty trigger_type")
    trigger_type = raw_trigger_type.strip()
    if trigger_type not in {"cron", "webhook", "poll", "pubsub"}:
        raise ConnectorDefinitionError(f"{source}: automation trigger_type '{trigger_type}' is not supported")
    raw_enabled = payload.get("enabled")
    if raw_enabled is not None and not isinstance(raw_enabled, bool):
        raise ConnectorDefinitionError(f"{source}: automation trigger enabled must be a boolean")
    raw_schedule = payload.get("schedule")
    raw_endpoint = payload.get("endpoint")
    raw_topic = payload.get("topic")
    for field_name, value in {"schedule": raw_schedule, "endpoint": raw_endpoint, "topic": raw_topic}.items():
        if value is not None and not isinstance(value, str):
            raise ConnectorDefinitionError(f"{source}: automation {field_name} must be a string")
    normalized_endpoint = raw_endpoint.strip() if isinstance(raw_endpoint, str) else ""
    if trigger_type == "webhook":
        canonical_endpoint = f"/api/automation/webhooks/{name}"
        if normalized_endpoint and normalized_endpoint != canonical_endpoint:
            raise ConnectorDefinitionError(
                f"{source}: webhook endpoint must be '{canonical_endpoint}'"
            )
        normalized_endpoint = canonical_endpoint
    else:
        normalized_endpoint = ""
    requires_network = _parse_optional_bool(payload.get("requires_network"), source=source, field_name="requires_network")
    config_fields = _parse_config_fields(payload.get("config_fields"), source=source)
    if trigger_type == "webhook":
        config_fields = _ensure_config_field(
            config_fields,
            key="signing_secret",
            label="Signing Secret",
            input_kind="password",
        )
    return AutomationTriggerDefinition(
        name=name,
        trigger_type=trigger_type,
        description=description,
        enabled=False if raw_enabled is None else raw_enabled,
        schedule=raw_schedule.strip() if isinstance(raw_schedule, str) else "",
        endpoint=normalized_endpoint,
        topic=raw_topic.strip() if isinstance(raw_topic, str) else "",
        capabilities=_parse_string_list(payload.get("capabilities"), source=source, field_name="capabilities"),
        config_fields=config_fields,
        requires_network=(trigger_type in {"webhook", "poll", "pubsub"}) if requires_network is None else requires_network,
    )


def parse_browser_provider_definition(payload: Any, *, source: str) -> BrowserProviderDefinition:
    name, description = _parse_named_object(payload, source=source, noun="browser provider")
    if not isinstance(payload, dict):
        raise ConnectorDefinitionError(f"{source}: browser provider definition must be an object")
    raw_provider_kind = payload.get("provider_kind")
    if not isinstance(raw_provider_kind, str) or not raw_provider_kind.strip():
        raise ConnectorDefinitionError(f"{source}: browser provider must include a non-empty provider_kind")
    provider_kind = raw_provider_kind.strip()
    if provider_kind not in {"browserbase", "local", "remote_cdp", "extension_relay"}:
        raise ConnectorDefinitionError(f"{source}: browser provider_kind '{provider_kind}' is not supported")
    raw_enabled = payload.get("enabled")
    if raw_enabled is not None and not isinstance(raw_enabled, bool):
        raise ConnectorDefinitionError(f"{source}: browser provider enabled must be a boolean")
    requires_network = _parse_optional_bool(payload.get("requires_network"), source=source, field_name="requires_network")
    requires_daemon = _parse_optional_bool(payload.get("requires_daemon"), source=source, field_name="requires_daemon")
    return BrowserProviderDefinition(
        name=name,
        provider_kind=provider_kind,
        description=description,
        enabled=False if raw_enabled is None else raw_enabled,
        capabilities=_parse_string_list(payload.get("capabilities"), source=source, field_name="capabilities"),
        config_fields=_parse_config_fields(payload.get("config_fields"), source=source),
        requires_network=(provider_kind != "local") if requires_network is None else requires_network,
        requires_daemon=(provider_kind == "extension_relay") if requires_daemon is None else requires_daemon,
    )


def parse_messaging_connector_definition(payload: Any, *, source: str) -> MessagingConnectorDefinition:
    name, description = _parse_named_object(payload, source=source, noun="messaging connector")
    if not isinstance(payload, dict):
        raise ConnectorDefinitionError(f"{source}: messaging connector definition must be an object")
    raw_platform = payload.get("platform")
    if not isinstance(raw_platform, str) or not raw_platform.strip():
        raise ConnectorDefinitionError(f"{source}: messaging connector must include a non-empty platform")
    platform = raw_platform.strip()
    if platform not in {"telegram", "discord", "slack", "email", "matrix", "sms", "webhook"}:
        raise ConnectorDefinitionError(f"{source}: messaging platform '{platform}' is not supported")
    raw_enabled = payload.get("enabled")
    if raw_enabled is not None and not isinstance(raw_enabled, bool):
        raise ConnectorDefinitionError(f"{source}: messaging connector enabled must be a boolean")
    requires_network = _parse_optional_bool(payload.get("requires_network"), source=source, field_name="requires_network")
    return MessagingConnectorDefinition(
        name=name,
        platform=platform,
        description=description,
        enabled=False if raw_enabled is None else raw_enabled,
        delivery_modes=_parse_string_list(payload.get("delivery_modes"), source=source, field_name="delivery_modes"),
        config_fields=_parse_config_fields(payload.get("config_fields"), source=source),
        requires_network=True if requires_network is None else requires_network,
    )


def parse_speech_profile_definition(payload: Any, *, source: str) -> SpeechProfileDefinition:
    name, description = _parse_named_object(payload, source=source, noun="speech profile")
    if not isinstance(payload, dict):
        raise ConnectorDefinitionError(f"{source}: speech profile definition must be an object")
    raw_provider = payload.get("provider")
    if not isinstance(raw_provider, str) or not raw_provider.strip():
        raise ConnectorDefinitionError(f"{source}: speech profile must include a non-empty provider")
    raw_voice = payload.get("voice")
    raw_wake_word = payload.get("wake_word")
    for field_name, value in {"voice": raw_voice, "wake_word": raw_wake_word}.items():
        if value is not None and not isinstance(value, str):
            raise ConnectorDefinitionError(f"{source}: speech profile {field_name} must be a string")
    raw_supports_tts = payload.get("supports_tts")
    raw_supports_stt = payload.get("supports_stt")
    if raw_supports_tts is not None and not isinstance(raw_supports_tts, bool):
        raise ConnectorDefinitionError(f"{source}: speech profile supports_tts must be a boolean")
    if raw_supports_stt is not None and not isinstance(raw_supports_stt, bool):
        raise ConnectorDefinitionError(f"{source}: speech profile supports_stt must be a boolean")
    supports_tts = bool(raw_supports_tts)
    supports_stt = bool(raw_supports_stt)
    if not supports_tts and not supports_stt:
        raise ConnectorDefinitionError(
            f"{source}: speech profile must support at least one of TTS or STT"
        )
    return SpeechProfileDefinition(
        name=name,
        provider=raw_provider.strip(),
        description=description,
        voice=raw_voice.strip() if isinstance(raw_voice, str) else "",
        supports_tts=supports_tts,
        supports_stt=supports_stt,
        wake_word=raw_wake_word.strip() if isinstance(raw_wake_word, str) else "",
    )


def parse_provider_preset_definition(payload: Any, *, source: str) -> ProviderPresetDefinition:
    name, _description = _parse_named_object(payload, source=source, noun="provider preset")
    if not isinstance(payload, dict):
        raise ConnectorDefinitionError(f"{source}: provider preset definition must be an object")
    raw_label = payload.get("label")
    raw_default_model = payload.get("default_model")
    raw_notes = payload.get("notes")
    for field_name, value in {
        "label": raw_label,
        "default_model": raw_default_model,
        "notes": raw_notes,
    }.items():
        if value is not None and not isinstance(value, str):
            raise ConnectorDefinitionError(f"{source}: provider preset {field_name} must be a string")
    if not isinstance(raw_default_model, str) or not raw_default_model.strip():
        raise ConnectorDefinitionError(f"{source}: provider preset must include a non-empty default_model")
    return ProviderPresetDefinition(
        name=name,
        label=raw_label.strip() if isinstance(raw_label, str) else "",
        default_model=raw_default_model.strip(),
        notes=raw_notes.strip() if isinstance(raw_notes, str) else "",
    )


def parse_canvas_output_definition(payload: Any, *, source: str) -> CanvasOutputDefinition:
    name, description = _parse_named_object(payload, source=source, noun="canvas output")
    if not isinstance(payload, dict):
        raise ConnectorDefinitionError(f"{source}: canvas output definition must be an object")
    raw_title = payload.get("title")
    if not isinstance(raw_title, str) or not raw_title.strip():
        raise ConnectorDefinitionError(f"{source}: canvas output must include a non-empty title")
    raw_surface_kind = payload.get("surface_kind")
    if raw_surface_kind is not None and not isinstance(raw_surface_kind, str):
        raise ConnectorDefinitionError(f"{source}: canvas output surface_kind must be a string")
    surface_kind = raw_surface_kind.strip() if isinstance(raw_surface_kind, str) and raw_surface_kind.strip() else "board"
    if surface_kind not in {"board", "report", "checklist", "gallery"}:
        raise ConnectorDefinitionError(f"{source}: canvas output surface_kind '{surface_kind}' is not supported")
    raw_preferred_panel = payload.get("preferred_panel")
    if raw_preferred_panel is not None and not isinstance(raw_preferred_panel, str):
        raise ConnectorDefinitionError(f"{source}: canvas output preferred_panel must be a string")
    sections = _parse_string_list(payload.get("sections"), source=source, field_name="sections")
    artifact_types = _parse_string_list(payload.get("artifact_types"), source=source, field_name="artifact_types")
    if not any((description, sections, artifact_types)):
        raise ConnectorDefinitionError(
            f"{source}: canvas output must declare description, sections, or artifact_types"
        )
    return CanvasOutputDefinition(
        name=name,
        title=raw_title.strip(),
        description=description,
        surface_kind=surface_kind,
        sections=sections,
        artifact_types=artifact_types,
        preferred_panel=raw_preferred_panel.strip() if isinstance(raw_preferred_panel, str) else "",
    )


def parse_workflow_runtime_definition(payload: Any, *, source: str) -> WorkflowRuntimeDefinition:
    name, description = _parse_named_object(payload, source=source, noun="workflow runtime")
    if not isinstance(payload, dict):
        raise ConnectorDefinitionError(f"{source}: workflow runtime definition must be an object")
    raw_engine_kind = payload.get("engine_kind")
    if not isinstance(raw_engine_kind, str) or not raw_engine_kind.strip():
        raise ConnectorDefinitionError(f"{source}: workflow runtime must include a non-empty engine_kind")
    engine_kind = raw_engine_kind.strip()
    if engine_kind not in {"openprose", "lobster", "llm_task"}:
        raise ConnectorDefinitionError(f"{source}: workflow runtime engine_kind '{engine_kind}' is not supported")
    raw_delegation_mode = payload.get("delegation_mode")
    raw_checkpoint_policy = payload.get("checkpoint_policy")
    raw_default_output_surface = payload.get("default_output_surface")
    for field_name, value in {
        "delegation_mode": raw_delegation_mode,
        "checkpoint_policy": raw_checkpoint_policy,
        "default_output_surface": raw_default_output_surface,
    }.items():
        if value is not None and not isinstance(value, str):
            raise ConnectorDefinitionError(f"{source}: workflow runtime {field_name} must be a string")
    delegation_mode = raw_delegation_mode.strip() if isinstance(raw_delegation_mode, str) else ""
    if delegation_mode and delegation_mode not in {"inline", "delegate", "multi_agent"}:
        raise ConnectorDefinitionError(
            f"{source}: workflow runtime delegation_mode '{delegation_mode}' is not supported"
        )
    checkpoint_policy = raw_checkpoint_policy.strip() if isinstance(raw_checkpoint_policy, str) else ""
    if checkpoint_policy and checkpoint_policy not in {"manual", "step", "approval"}:
        raise ConnectorDefinitionError(
            f"{source}: workflow runtime checkpoint_policy '{checkpoint_policy}' is not supported"
        )
    raw_structured_output = payload.get("structured_output")
    if raw_structured_output is not None and not isinstance(raw_structured_output, bool):
        raise ConnectorDefinitionError(f"{source}: workflow runtime structured_output must be a boolean")
    return WorkflowRuntimeDefinition(
        name=name,
        engine_kind=engine_kind,
        description=description,
        delegation_mode=delegation_mode,
        checkpoint_policy=checkpoint_policy,
        structured_output=bool(raw_structured_output),
        default_output_surface=raw_default_output_surface.strip() if isinstance(raw_default_output_surface, str) else "",
    )


def parse_node_adapter_definition(payload: Any, *, source: str) -> NodeAdapterDefinition:
    name, description = _parse_named_object(payload, source=source, noun="node adapter")
    if not isinstance(payload, dict):
        raise ConnectorDefinitionError(f"{source}: node adapter definition must be an object")
    raw_adapter_kind = payload.get("adapter_kind")
    if not isinstance(raw_adapter_kind, str) or not raw_adapter_kind.strip():
        raise ConnectorDefinitionError(f"{source}: node adapter must include a non-empty adapter_kind")
    adapter_kind = raw_adapter_kind.strip()
    if adapter_kind not in {"companion", "canvas", "device", "camera", "notification"}:
        raise ConnectorDefinitionError(f"{source}: node adapter_kind '{adapter_kind}' is not supported")
    raw_enabled = payload.get("enabled")
    if raw_enabled is not None and not isinstance(raw_enabled, bool):
        raise ConnectorDefinitionError(f"{source}: node adapter enabled must be a boolean")
    requires_network = _parse_optional_bool(payload.get("requires_network"), source=source, field_name="requires_network")
    requires_daemon = _parse_optional_bool(payload.get("requires_daemon"), source=source, field_name="requires_daemon")
    return NodeAdapterDefinition(
        name=name,
        adapter_kind=adapter_kind,
        description=description,
        enabled=False if raw_enabled is None else raw_enabled,
        capabilities=_parse_string_list(payload.get("capabilities"), source=source, field_name="capabilities"),
        config_fields=_parse_config_fields(payload.get("config_fields"), source=source),
        requires_network=(adapter_kind != "canvas") if requires_network is None else requires_network,
        requires_daemon=(adapter_kind != "canvas") if requires_daemon is None else requires_daemon,
    )


def load_toolset_preset_definition(path: Path) -> ToolsetPresetDefinition:
    return parse_toolset_preset_definition(load_connector_payload(path), source=str(path))


def load_context_pack_definition(path: Path) -> ContextPackDefinition:
    return parse_context_pack_definition(load_connector_payload(path), source=str(path))


def load_prompt_pack_definition(path: Path) -> PromptPackDefinition:
    return parse_prompt_pack_definition(path.read_text(encoding="utf-8"), source=str(path))


def load_automation_trigger_definition(path: Path) -> AutomationTriggerDefinition:
    return parse_automation_trigger_definition(load_connector_payload(path), source=str(path))


def load_browser_provider_definition(path: Path) -> BrowserProviderDefinition:
    return parse_browser_provider_definition(load_connector_payload(path), source=str(path))


def load_messaging_connector_definition(path: Path) -> MessagingConnectorDefinition:
    return parse_messaging_connector_definition(load_connector_payload(path), source=str(path))


def load_speech_profile_definition(path: Path) -> SpeechProfileDefinition:
    return parse_speech_profile_definition(load_connector_payload(path), source=str(path))


def load_provider_preset_definition(path: Path) -> ProviderPresetDefinition:
    return parse_provider_preset_definition(load_connector_payload(path), source=str(path))


def load_canvas_output_definition(path: Path) -> CanvasOutputDefinition:
    return parse_canvas_output_definition(load_connector_payload(path), source=str(path))


def load_workflow_runtime_definition(path: Path) -> WorkflowRuntimeDefinition:
    return parse_workflow_runtime_definition(load_connector_payload(path), source=str(path))


def load_node_adapter_definition(path: Path) -> NodeAdapterDefinition:
    return parse_node_adapter_definition(load_connector_payload(path), source=str(path))
