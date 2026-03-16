"""Shared LLM configuration and fallback helpers."""

from __future__ import annotations

import asyncio
import contextvars
import logging
from threading import Lock
from time import monotonic
from typing import Any
from uuid import uuid4

from smolagents import LiteLLMModel as BaseLiteLLMModel

from config.settings import settings
from src.audit.repository import audit_repository

logger = logging.getLogger(__name__)
_runtime_request_lock = Lock()
_runtime_requests: dict[str, bool] = {}
_target_health_lock = Lock()
_unhealthy_targets: dict[tuple[str, str | None, str | None], float] = {}
_runtime_request_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "llm_runtime_request_id",
    default=None,
)


def _primary_api_key() -> str:
    return settings.llm_api_key or settings.openrouter_api_key


def has_local_model_profile() -> bool:
    """Return whether a local-model profile is configured."""
    return bool(settings.local_model.strip())


def local_runtime_paths() -> set[str]:
    """Return the configured runtime-path names that should prefer the local profile."""
    return {
        path.strip()
        for path in settings.local_runtime_paths.split(",")
        if path.strip()
    }


def _runtime_model_override(runtime_path: str | None) -> tuple[str | None, str] | None:
    """Return an optional runtime-path-specific profile/model override."""
    if not runtime_path:
        return None
    for raw_entry in settings.runtime_model_overrides.split(","):
        entry = raw_entry.strip()
        if not entry or "=" not in entry:
            continue
        path, _, raw_value = entry.partition("=")
        if path.strip() != runtime_path:
            continue
        value = raw_value.strip()
        if not value:
            return None
        if ":" in value:
            maybe_profile, model_id = value.split(":", 1)
            normalized_profile = maybe_profile.strip()
            normalized_model_id = model_id.strip()
            if normalized_profile in {"default", "local"} and normalized_model_id:
                return normalized_profile, normalized_model_id
        return None, value
    return None


def resolve_runtime_profile(
    *,
    runtime_path: str | None = None,
    profile: str | None = None,
) -> str:
    """Resolve the runtime profile name for the current call."""
    if profile:
        return profile
    override = _runtime_model_override(runtime_path)
    if override and override[0]:
        return override[0]
    if runtime_path and runtime_path in local_runtime_paths() and has_local_model_profile():
        return "local"
    return "default"


def _resolved_primary_model_id(
    *,
    runtime_path: str | None = None,
    profile: str = "default",
) -> str:
    override = _runtime_model_override(runtime_path)
    if override:
        return override[1]
    return _profile_model_id(profile)


def _profile_model_id(profile: str) -> str:
    if profile == "local" and has_local_model_profile():
        return settings.local_model
    return settings.default_model


def _profile_api_key(profile: str) -> str:
    if profile == "local" and has_local_model_profile():
        return settings.local_llm_api_key or _primary_api_key()
    return _primary_api_key()


def _profile_api_base(profile: str) -> str:
    if profile == "local" and has_local_model_profile():
        return settings.local_llm_api_base or settings.llm_api_base
    return settings.llm_api_base


def fallback_model_ids() -> list[str]:
    """Return the ordered list of configured fallback model ids."""
    fallback_ids: list[str] = []
    for raw_value in (settings.fallback_model, settings.fallback_models):
        for model_id in raw_value.split(","):
            normalized = model_id.strip()
            if normalized and normalized not in fallback_ids:
                fallback_ids.append(normalized)
    return fallback_ids


def build_model_kwargs(
    *,
    temperature: float,
    max_tokens: int,
    model_id: str | None = None,
    runtime_path: str | None = None,
    profile: str | None = None,
) -> dict[str, Any]:
    """Build LiteLLMModel kwargs from the shared runtime settings."""
    resolved_profile = resolve_runtime_profile(runtime_path=runtime_path, profile=profile)
    kwargs: dict[str, Any] = {
        "model_id": model_id or _resolved_primary_model_id(
            runtime_path=runtime_path,
            profile=resolved_profile,
        ),
        "temperature": temperature,
        "max_tokens": max_tokens,
        "runtime_profile": resolved_profile,
    }
    api_key = _profile_api_key(resolved_profile)
    if api_key:
        kwargs["api_key"] = api_key
    api_base = _profile_api_base(resolved_profile)
    if api_base:
        kwargs["api_base"] = api_base
    return kwargs


def build_completion_kwargs(
    *,
    messages: list[dict[str, str]],
    temperature: float,
    max_tokens: int,
    model_id: str | None = None,
    use_fallback: bool = False,
    fallback_model_id: str | None = None,
    fallback_api_key: str | None = None,
    fallback_api_base: str | None = None,
    runtime_path: str | None = None,
    profile: str | None = None,
) -> dict[str, Any]:
    """Build litellm.completion kwargs for either the primary or fallback path."""
    if use_fallback:
        fallback_model = fallback_model_id or next(iter(fallback_model_ids()), "")
        if not fallback_model:
            raise ValueError("Fallback model is not configured")
        kwargs: dict[str, Any] = {
            "model": model_id or fallback_model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        api_key = fallback_api_key or settings.fallback_llm_api_key or _primary_api_key()
        api_base = fallback_api_base or settings.fallback_llm_api_base or settings.llm_api_base
    else:
        resolved_profile = resolve_runtime_profile(runtime_path=runtime_path, profile=profile)
        kwargs = {
            "model": model_id or _resolved_primary_model_id(
                runtime_path=runtime_path,
                profile=resolved_profile,
            ),
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        api_key = _profile_api_key(resolved_profile)
        api_base = _profile_api_base(resolved_profile)

    if api_key:
        kwargs["api_key"] = api_key
    if api_base:
        kwargs["api_base"] = api_base
    return kwargs


def has_fallback_model() -> bool:
    """Return whether a fallback completion target is configured."""
    return bool(fallback_model_ids())


def _fallback_api_key(primary_api_key: str | None, *, primary_profile: str = "default") -> str:
    if settings.fallback_llm_api_key:
        return settings.fallback_llm_api_key
    if primary_profile == "local":
        return _primary_api_key()
    return primary_api_key or _primary_api_key()


def _fallback_api_base(primary_api_base: str | None, *, primary_profile: str = "default") -> str:
    if settings.fallback_llm_api_base:
        return settings.fallback_llm_api_base
    if primary_profile == "local":
        return settings.llm_api_base
    return primary_api_base or settings.llm_api_base


def _safe_model_name(kwargs: dict[str, Any]) -> str:
    return str(kwargs.get("model", "unknown"))


def _target_key(*, model_id: str, api_base: str | None, api_key: str | None) -> tuple[str, str | None, str | None]:
    return (model_id, api_base or None, api_key or None)


def _fallback_targets(
    *,
    primary_model_id: str,
    primary_api_base: str | None,
    primary_api_key: str | None,
    primary_profile: str = "default",
) -> list[dict[str, str | None]]:
    fallback_api_key = _fallback_api_key(primary_api_key, primary_profile=primary_profile)
    fallback_api_base = _fallback_api_base(primary_api_base, primary_profile=primary_profile)
    seen_targets = {
        _target_key(
            model_id=primary_model_id,
            api_base=primary_api_base,
            api_key=primary_api_key,
        ),
    }
    targets: list[dict[str, str | None]] = []
    for fallback_model_id in fallback_model_ids():
        target_key = _target_key(
            model_id=fallback_model_id,
            api_base=fallback_api_base,
            api_key=fallback_api_key,
        )
        if target_key in seen_targets:
            continue
        seen_targets.add(target_key)
        targets.append(
            {
                "model_id": fallback_model_id,
                "api_base": fallback_api_base,
                "api_key": fallback_api_key,
            }
        )
    return targets


def _target_cooldown_seconds() -> int:
    return max(0, settings.llm_target_cooldown_seconds)


def _reset_target_health() -> None:
    with _target_health_lock:
        _unhealthy_targets.clear()


def _mark_target_failed(
    *,
    model_id: str,
    api_base: str | None,
    api_key: str | None,
) -> None:
    cooldown_seconds = _target_cooldown_seconds()
    if cooldown_seconds <= 0:
        return
    with _target_health_lock:
        _unhealthy_targets[_target_key(model_id=model_id, api_base=api_base, api_key=api_key)] = (
            monotonic() + cooldown_seconds
        )


def _mark_target_succeeded(
    *,
    model_id: str,
    api_base: str | None,
    api_key: str | None,
) -> None:
    with _target_health_lock:
        _unhealthy_targets.pop(
            _target_key(model_id=model_id, api_base=api_base, api_key=api_key),
            None,
        )


def _is_target_healthy(
    *,
    model_id: str,
    api_base: str | None,
    api_key: str | None,
) -> bool:
    target_key = _target_key(model_id=model_id, api_base=api_base, api_key=api_key)
    with _target_health_lock:
        unhealthy_until = _unhealthy_targets.get(target_key)
        if unhealthy_until is None:
            return True
        if unhealthy_until <= monotonic():
            _unhealthy_targets.pop(target_key, None)
            return True
        return False


def _partition_targets_by_health(
    targets: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    healthy_targets: list[dict[str, Any]] = []
    unhealthy_targets: list[dict[str, Any]] = []
    for target in targets:
        if _is_target_healthy(
            model_id=str(target["model_id"]),
            api_base=target.get("api_base"),
            api_key=target.get("api_key"),
        ):
            healthy_targets.append(target)
        else:
            unhealthy_targets.append(target)
    return healthy_targets, unhealthy_targets


def _register_request(request_id: str) -> None:
    with _runtime_request_lock:
        _runtime_requests[request_id] = False


def _mark_request_timed_out(request_id: str) -> None:
    with _runtime_request_lock:
        if request_id in _runtime_requests:
            _runtime_requests[request_id] = True


def _finish_request(request_id: str) -> None:
    with _runtime_request_lock:
        _runtime_requests.pop(request_id, None)


def _can_log_request(request_id: str | None) -> bool:
    if request_id is None:
        return True
    with _runtime_request_lock:
        timed_out = _runtime_requests.get(request_id)
    if timed_out is None:
        return False
    return not timed_out


def set_current_llm_request_id(request_id: str) -> contextvars.Token[str | None]:
    """Bind an LLM runtime request id to the current context."""
    return _runtime_request_id_var.set(request_id)


def reset_current_llm_request_id(token: contextvars.Token[str | None]) -> None:
    """Restore the previous LLM runtime request id for the current context."""
    _runtime_request_id_var.reset(token)


def _current_llm_request_id() -> str | None:
    return _runtime_request_id_var.get()


async def _log_llm_runtime_event(
    *,
    event_type: str,
    summary: str,
    details: dict[str, Any],
) -> None:
    try:
        await audit_repository.log_event(
            event_type=event_type,
            actor="system",
            tool_name="llm_runtime",
            risk_level="low",
            policy_mode="full",
            summary=summary,
            details=details,
        )
    except Exception:
        logger.debug("Failed to record LLM runtime audit event", exc_info=True)


def _log_llm_runtime_event_sync(
    *,
    event_type: str,
    summary: str,
    details: dict[str, Any],
) -> None:
    try:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(_log_llm_runtime_event(
                event_type=event_type,
                summary=summary,
                details=details,
            ))
            return

        loop.create_task(_log_llm_runtime_event(
            event_type=event_type,
            summary=summary,
            details=details,
        ))
    except Exception:
        logger.debug("Failed to run LLM runtime audit logger", exc_info=True)


class FallbackLiteLLMModel(BaseLiteLLMModel):
    """LiteLLM model wrapper that retries via the configured fallback model."""

    def __init__(
        self,
        model_id: str | None = None,
        api_base: str | None = None,
        api_key: str | None = None,
        custom_role_conversions: dict[str, str] | None = None,
        flatten_messages_as_text: bool | None = None,
        runtime_profile: str | None = None,
        **kwargs,
    ):
        self._runtime_profile = runtime_profile or "default"
        super().__init__(
            model_id=model_id,
            api_base=api_base,
            api_key=api_key,
            custom_role_conversions=custom_role_conversions,
            flatten_messages_as_text=flatten_messages_as_text,
            **kwargs,
        )
        self._fallback_models: tuple[BaseLiteLLMModel, ...] = ()
        self._fallback_model: BaseLiteLLMModel | None = None
        fallback_kwargs = dict(kwargs)
        if flatten_messages_as_text is not None:
            fallback_kwargs["flatten_messages_as_text"] = flatten_messages_as_text

        fallback_models: list[BaseLiteLLMModel] = []
        for target in _fallback_targets(
            primary_model_id=self.model_id,
            primary_api_base=self.api_base,
            primary_api_key=self.api_key,
            primary_profile=self._runtime_profile,
        ):
            fallback_models.append(
                BaseLiteLLMModel(
                    model_id=str(target["model_id"]),
                    api_base=target["api_base"] or None,
                    api_key=target["api_key"] or None,
                    custom_role_conversions=custom_role_conversions,
                    **fallback_kwargs,
                )
            )

        self._fallback_models = tuple(fallback_models)
        self._fallback_model = self._fallback_models[0] if self._fallback_models else None

    def generate(
        self,
        messages,
        stop_sequences=None,
        response_format=None,
        tools_to_call_from=None,
        **kwargs,
    ):
        primary_model = self.model_id
        request_id = _current_llm_request_id()
        fallback_targets = [
            {
                "model_id": fallback_model.model_id,
                "api_base": fallback_model.api_base,
                "api_key": fallback_model.api_key,
                "model": fallback_model,
            }
            for fallback_model in self._fallback_models
        ]
        healthy_fallbacks, unhealthy_fallbacks = _partition_targets_by_health(fallback_targets)
        primary_unhealthy = not _is_target_healthy(
            model_id=primary_model,
            api_base=self.api_base,
            api_key=self.api_key,
        )
        rerouted = primary_unhealthy and bool(healthy_fallbacks)

        if rerouted and _can_log_request(request_id):
            _log_llm_runtime_event_sync(
                event_type="llm_target_rerouted",
                summary=(
                    f"Agent model generate rerouted from unhealthy {primary_model} "
                    f"to {healthy_fallbacks[0]['model_id']}"
                ),
                details={
                    "runtime_path": "agent_generate",
                    "primary_model": primary_model,
                    "rerouted_model": healthy_fallbacks[0]["model_id"],
                    "unhealthy_models": [primary_model],
                    "cooldown_seconds": _target_cooldown_seconds(),
                },
            )

        ordered_fallbacks = healthy_fallbacks + unhealthy_fallbacks
        primary_attempted = False
        primary_error: Exception | None = None
        attempted_fallback_models: list[str] = []
        fallback_errors: list[dict[str, str]] = []

        if not rerouted:
            try:
                response = super().generate(
                    messages,
                    stop_sequences=stop_sequences,
                    response_format=response_format,
                    tools_to_call_from=tools_to_call_from,
                    **kwargs,
                )
                _mark_target_succeeded(
                    model_id=primary_model,
                    api_base=self.api_base,
                    api_key=self.api_key,
                )
                if _can_log_request(request_id):
                    _log_llm_runtime_event_sync(
                        event_type="llm_primary_success",
                        summary=f"Primary agent model generate succeeded via {primary_model}",
                        details={
                            "runtime_path": "agent_generate",
                            "primary_model": primary_model,
                            "used_fallback": False,
                        },
                    )
                return response
            except Exception as error:
                primary_attempted = True
                primary_error = error
                _mark_target_failed(
                    model_id=primary_model,
                    api_base=self.api_base,
                    api_key=self.api_key,
                )
                if not ordered_fallbacks:
                    if _can_log_request(request_id):
                        _log_llm_runtime_event_sync(
                            event_type="llm_primary_failure",
                            summary=f"Primary agent model generate failed via {primary_model}",
                            details={
                                "runtime_path": "agent_generate",
                                "primary_model": primary_model,
                                "used_fallback": False,
                                "error": str(error),
                            },
                        )
                    raise
                logger.warning(
                    "LLM generate failed for %s, retrying with fallback %s",
                    primary_model,
                    ordered_fallbacks[0]["model_id"],
                    exc_info=True,
                )

        last_error: Exception = primary_error or RuntimeError("No fallback targets available")

        for index, target in enumerate(ordered_fallbacks):
            fallback_model = target["model"]
            attempted_fallback_models.append(fallback_model.model_id)
            try:
                response = fallback_model.generate(
                    messages,
                    stop_sequences=stop_sequences,
                    response_format=response_format,
                    tools_to_call_from=tools_to_call_from,
                    **kwargs,
                )
                _mark_target_succeeded(
                    model_id=fallback_model.model_id,
                    api_base=fallback_model.api_base,
                    api_key=fallback_model.api_key,
                )
                if _can_log_request(request_id):
                    details = {
                        "runtime_path": "agent_generate",
                        "primary_model": primary_model,
                        "fallback_model": fallback_model.model_id,
                        "attempted_fallback_models": attempted_fallback_models,
                        "fallback_attempts": len(attempted_fallback_models),
                        "used_fallback": True,
                        "primary_attempted": primary_attempted,
                    }
                    if primary_error is not None:
                        details["primary_error"] = str(primary_error)
                    if rerouted:
                        details["rerouted_from_unhealthy_primary"] = True
                    _log_llm_runtime_event_sync(
                        event_type="llm_fallback_success",
                        summary=f"Fallback agent model generate succeeded via {fallback_model.model_id}",
                        details=details,
                    )
                return response
            except Exception as fallback_error:
                last_error = fallback_error
                _mark_target_failed(
                    model_id=fallback_model.model_id,
                    api_base=fallback_model.api_base,
                    api_key=fallback_model.api_key,
                )
                fallback_errors.append(
                    {
                        "model": fallback_model.model_id,
                        "error": str(fallback_error),
                    }
                )
                if index + 1 < len(ordered_fallbacks):
                    logger.warning(
                        "Fallback model %s failed, retrying with next fallback %s",
                        fallback_model.model_id,
                        ordered_fallbacks[index + 1]["model_id"],
                        exc_info=True,
                    )

        if attempted_fallback_models and _can_log_request(request_id):
            details = {
                "runtime_path": "agent_generate",
                "primary_model": primary_model,
                "fallback_model": attempted_fallback_models[-1],
                "attempted_fallback_models": attempted_fallback_models,
                "fallback_attempts": len(attempted_fallback_models),
                "used_fallback": True,
                "fallback_error": str(last_error),
                "fallback_errors": fallback_errors,
                "primary_attempted": primary_attempted,
            }
            if primary_error is not None:
                details["primary_error"] = str(primary_error)
            if rerouted:
                details["rerouted_from_unhealthy_primary"] = True
            _log_llm_runtime_event_sync(
                event_type="llm_fallback_failure",
                summary=f"Fallback agent model generate failed via {attempted_fallback_models[-1]}",
                details=details,
            )
        raise last_error


def completion_with_fallback_sync(
    *,
    messages: list[dict[str, str]],
    temperature: float,
    max_tokens: int,
    model_id: str | None = None,
    request_id: str | None = None,
    runtime_path: str = "completion",
    profile: str | None = None,
):
    """Execute a litellm completion with an optional fallback target."""
    import litellm

    try:
        resolved_profile = resolve_runtime_profile(runtime_path=runtime_path, profile=profile)
        primary_kwargs = build_completion_kwargs(
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
            model_id=model_id,
            runtime_path=runtime_path,
            profile=resolved_profile,
        )
        primary_model = _safe_model_name(primary_kwargs)
        fallback_targets = _fallback_targets(
            primary_model_id=primary_model,
            primary_api_base=primary_kwargs.get("api_base"),
            primary_api_key=primary_kwargs.get("api_key"),
            primary_profile=resolved_profile,
        )
        healthy_fallbacks, unhealthy_fallbacks = _partition_targets_by_health(fallback_targets)
        primary_unhealthy = not _is_target_healthy(
            model_id=primary_model,
            api_base=primary_kwargs.get("api_base"),
            api_key=primary_kwargs.get("api_key"),
        )
        rerouted = primary_unhealthy and bool(healthy_fallbacks)

        if rerouted and _can_log_request(request_id):
            _log_llm_runtime_event_sync(
                event_type="llm_target_rerouted",
                summary=(
                    f"LLM completion rerouted from unhealthy {primary_model} "
                    f"to {healthy_fallbacks[0]['model_id']}"
                ),
                details={
                    "runtime_path": runtime_path,
                    "runtime_profile": resolved_profile,
                    "primary_model": primary_model,
                    "rerouted_model": healthy_fallbacks[0]["model_id"],
                    "unhealthy_models": [primary_model],
                    "cooldown_seconds": _target_cooldown_seconds(),
                },
            )

        ordered_fallbacks = healthy_fallbacks + unhealthy_fallbacks
        primary_attempted = False
        primary_error: Exception | None = None
        attempted_fallback_models: list[str] = []
        fallback_errors: list[dict[str, str]] = []

        if not rerouted:
            try:
                response = litellm.completion(**primary_kwargs)
                _mark_target_succeeded(
                    model_id=primary_model,
                    api_base=primary_kwargs.get("api_base"),
                    api_key=primary_kwargs.get("api_key"),
                )
                if _can_log_request(request_id):
                    _log_llm_runtime_event_sync(
                        event_type="llm_primary_success",
                        summary=f"Primary LLM completion succeeded via {primary_model}",
                        details={
                            "runtime_path": runtime_path,
                            "runtime_profile": resolved_profile,
                            "primary_model": primary_model,
                            "used_fallback": False,
                        },
                    )
                return response
            except Exception as error:
                primary_attempted = True
                primary_error = error
                _mark_target_failed(
                    model_id=primary_model,
                    api_base=primary_kwargs.get("api_base"),
                    api_key=primary_kwargs.get("api_key"),
                )
                if not ordered_fallbacks:
                    if _can_log_request(request_id):
                        _log_llm_runtime_event_sync(
                            event_type="llm_primary_failure",
                            summary=f"Primary LLM completion failed via {primary_model}",
                            details={
                                "runtime_path": runtime_path,
                                "runtime_profile": resolved_profile,
                                "primary_model": primary_model,
                                "used_fallback": False,
                                "error": str(error),
                            },
                        )
                    raise
                logger.warning(
                    "LLM completion failed for model %s, retrying with fallback %s",
                    primary_model,
                    ordered_fallbacks[0]["model_id"],
                    exc_info=True,
                )

        last_error: Exception = primary_error or RuntimeError("No fallback targets available")

        for index, target in enumerate(ordered_fallbacks):
            fallback_kwargs = build_completion_kwargs(
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
                use_fallback=True,
                fallback_model_id=str(target["model_id"]),
                fallback_api_key=target["api_key"],
                fallback_api_base=target["api_base"],
            )
            fallback_model = _safe_model_name(fallback_kwargs)
            attempted_fallback_models.append(fallback_model)
            try:
                response = litellm.completion(**fallback_kwargs)
                _mark_target_succeeded(
                    model_id=fallback_model,
                    api_base=target["api_base"],
                    api_key=target["api_key"],
                )
                if _can_log_request(request_id):
                    details = {
                        "runtime_path": runtime_path,
                        "runtime_profile": resolved_profile,
                        "primary_model": primary_model,
                        "fallback_model": fallback_model,
                        "attempted_fallback_models": attempted_fallback_models,
                        "fallback_attempts": len(attempted_fallback_models),
                        "used_fallback": True,
                        "primary_attempted": primary_attempted,
                    }
                    if primary_error is not None:
                        details["primary_error"] = str(primary_error)
                    if rerouted:
                        details["rerouted_from_unhealthy_primary"] = True
                    _log_llm_runtime_event_sync(
                        event_type="llm_fallback_success",
                        summary=f"Fallback LLM completion succeeded via {fallback_model}",
                        details=details,
                    )
                return response
            except Exception as fallback_error:
                last_error = fallback_error
                _mark_target_failed(
                    model_id=fallback_model,
                    api_base=target["api_base"],
                    api_key=target["api_key"],
                )
                fallback_errors.append(
                    {
                        "model": fallback_model,
                        "error": str(fallback_error),
                    }
                )
                if index + 1 < len(ordered_fallbacks):
                    logger.warning(
                        "Fallback model %s failed, retrying with next fallback %s",
                        fallback_model,
                        ordered_fallbacks[index + 1]["model_id"],
                        exc_info=True,
                    )

        if attempted_fallback_models and _can_log_request(request_id):
            details = {
                "runtime_path": runtime_path,
                "runtime_profile": resolved_profile,
                "primary_model": primary_model,
                "fallback_model": attempted_fallback_models[-1],
                "attempted_fallback_models": attempted_fallback_models,
                "fallback_attempts": len(attempted_fallback_models),
                "used_fallback": True,
                "fallback_error": str(last_error),
                "fallback_errors": fallback_errors,
                "primary_attempted": primary_attempted,
            }
            if primary_error is not None:
                details["primary_error"] = str(primary_error)
            if rerouted:
                details["rerouted_from_unhealthy_primary"] = True
            _log_llm_runtime_event_sync(
                event_type="llm_fallback_failure",
                summary=f"Fallback LLM completion failed via {attempted_fallback_models[-1]}",
                details=details,
            )
        raise last_error
    finally:
        if request_id is not None:
            _finish_request(request_id)


async def completion_with_fallback(
    *,
    messages: list[dict[str, str]],
    temperature: float,
    max_tokens: int,
    timeout: int | None = None,
    model_id: str | None = None,
    runtime_path: str = "completion",
    profile: str | None = None,
):
    """Async wrapper around the shared completion fallback flow."""
    request_id = uuid4().hex
    _register_request(request_id)
    resolved_profile = resolve_runtime_profile(runtime_path=runtime_path, profile=profile)
    coro = asyncio.to_thread(
        completion_with_fallback_sync,
        messages=messages,
        temperature=temperature,
        max_tokens=max_tokens,
        model_id=model_id,
        request_id=request_id,
        runtime_path=runtime_path,
        profile=resolved_profile,
    )
    try:
        if timeout is None:
            return await coro
        return await asyncio.wait_for(coro, timeout=timeout)
    except asyncio.TimeoutError:
        _mark_request_timed_out(request_id)
        await _log_llm_runtime_event(
            event_type="llm_timed_out",
            summary=f"LLM completion timed out via {model_id or _profile_model_id(resolved_profile)}",
            details={
                "runtime_path": runtime_path,
                "runtime_profile": resolved_profile,
                "primary_model": model_id or _profile_model_id(resolved_profile),
                "timeout_seconds": timeout,
                "fallback_configured": has_fallback_model(),
            },
        )
        raise
    finally:
        if timeout is None:
            _finish_request(request_id)
