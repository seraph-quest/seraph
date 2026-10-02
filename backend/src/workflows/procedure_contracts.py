"""Strict, server-owned contracts for reviewed guardian procedures.

The v2 surface is deliberately a fixed registry. A procedure version stores
one canonical plan copied from verified source work; callers can select only
the approved goal/source/event parameters at invocation time.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
from typing import Any, Mapping

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


ROUTINE_V2_CAPABILITY_VERSION = "guardian-routine.v2"
ROUTINE_V2_STEP_IDS = {
    "public-browser-check": ("public_browser_check",),
    "watch-and-public-browser": ("source_watch", "public_browser_check"),
    "selected-meeting-prep": ("selected_meeting_prep",),
}

_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_REFERENCE_RE = re.compile(r"^[A-Za-z0-9_.:/-]{1,512}$")
_TEMPLATE_RE = re.compile(r"^[a-z0-9-]{1,80}$")


class ProcedureContractError(ValueError):
    """A strict procedure plan or template contract is invalid."""

    def __init__(self, code: str, message: str | None = None):
        self.code = code
        super().__init__(message or code)


class ProcedureV2Parameter(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    name: str = Field(min_length=1, max_length=128)
    kind: str = Field(min_length=1, max_length=64)
    required: bool


class ProcedureV2Step(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    step_id: str = Field(min_length=1, max_length=80)
    capability_id: str = Field(min_length=1, max_length=128)
    capability_version: str = Field(min_length=1, max_length=128)
    typed_input_ref: str = Field(min_length=1, max_length=512)
    typed_input_digest: str = Field(min_length=64, max_length=64)

    @field_validator("typed_input_digest")
    @classmethod
    def digest_is_sha256(cls, value: str) -> str:
        normalized = value.lower()
        if _DIGEST_RE.fullmatch(normalized) is None:
            raise ValueError("typed_input_digest must be a SHA-256 hexadecimal digest")
        return normalized

    @field_validator("typed_input_ref")
    @classmethod
    def reference_is_bounded(cls, value: str) -> str:
        if _REFERENCE_RE.fullmatch(value) is None or any(
            part in {"", ".", ".."} for part in value.split("/")
        ):
            raise ValueError("typed_input_ref must be a bounded workspace reference")
        return value


class ProcedureV2Limits(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    max_steps: int = Field(..., ge=1, le=2)
    max_total_seconds: int = Field(..., ge=1, le=300)


class ProcedureV2Plan(BaseModel):
    """Canonical immutable plan persisted in ``GuardianRoutineVersion``."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    schema_version: int = Field(..., ge=2, le=2)
    template_id: str = Field(min_length=1, max_length=80)
    steps: tuple[ProcedureV2Step, ...] = Field(min_length=1, max_length=2)
    parameters: tuple[ProcedureV2Parameter, ...] = Field(max_length=32)
    verifier: str = Field(min_length=1, max_length=64)
    limits: ProcedureV2Limits

    @field_validator("template_id")
    @classmethod
    def template_id_is_safe(cls, value: str) -> str:
        if _TEMPLATE_RE.fullmatch(value) is None:
            raise ValueError("template_id is invalid")
        return value

    @model_validator(mode="after")
    def canonical_contract(self) -> "ProcedureV2Plan":
        spec = PROCEDURE_V2_TEMPLATES.get(self.template_id)
        if spec is None:
            raise ValueError("procedure template is not registered")
        expected_ids = tuple(step.step_id for step in spec.steps)
        if tuple(step.step_id for step in self.steps) != expected_ids:
            raise ValueError("procedure step order does not match the registered template")
        for observed, expected in zip(self.steps, spec.steps):
            if (
                observed.capability_id != expected.capability_id
                or observed.capability_version != expected.capability_version
            ):
                raise ValueError("procedure step capability does not match the registered template")
        expected_parameters = tuple(spec.parameters)
        observed_parameters = tuple(
            (item.name, item.kind, item.required) for item in self.parameters
        )
        if observed_parameters != expected_parameters:
            raise ValueError("procedure parameter schema does not match the registered template")
        if self.verifier != "leaf_readbacks":
            raise ValueError("procedure verifier is invalid")
        if self.limits.max_steps != 2 or self.limits.max_total_seconds != 300:
            raise ValueError("procedure limits must be max_steps=2 and max_total_seconds=300")
        return self


@dataclass(frozen=True, slots=True)
class ProcedureV2StepSpec:
    step_id: str
    capability_id: str
    capability_version: str


@dataclass(frozen=True, slots=True)
class ProcedureV2TemplateSpec:
    template_id: str
    steps: tuple[ProcedureV2StepSpec, ...]
    parameters: tuple[tuple[str, str, bool], ...]
    permissions: tuple[str, ...]
    schedulable: bool
    requires_material_change: bool = False

    @property
    def max_steps(self) -> int:
        return 2

    @property
    def max_total_seconds(self) -> int:
        return 300


PROCEDURE_V2_TEMPLATES: dict[str, ProcedureV2TemplateSpec] = {
    "public-browser-check": ProcedureV2TemplateSpec(
        template_id="public-browser-check",
        steps=(ProcedureV2StepSpec("public_browser_check", "browser.public-task.v1", "1"),),
        parameters=(("goal_id", "goal_id", True), ("expected_goal_revision", "goal_revision", True)),
        permissions=("browser.public-task.v1",),
        schedulable=True,
    ),
    "watch-and-public-browser": ProcedureV2TemplateSpec(
        template_id="watch-and-public-browser",
        steps=(
            ProcedureV2StepSpec("source_watch", "guardian.research-watch.v1", "1"),
            ProcedureV2StepSpec("public_browser_check", "browser.public-task.v1", "1"),
        ),
        parameters=(
            ("goal_id", "goal_id", True),
            ("expected_goal_revision", "goal_revision", True),
            ("source_watch_id", "source_watch_id", True),
            ("expected_watch_revision", "watch_revision", True),
        ),
        permissions=("guardian.research-watch.v1", "browser.public-task.v1"),
        schedulable=True,
        requires_material_change=True,
    ),
    "selected-meeting-prep": ProcedureV2TemplateSpec(
        template_id="selected-meeting-prep",
        steps=(ProcedureV2StepSpec("selected_meeting_prep", "calendar.meeting-prep.v1", "1"),),
        parameters=(
            ("schema_version", "schema_version", True),
            ("consent_id", "consent_id", True),
            ("event_binding_id", "event_binding_id", True),
            ("expected_event_binding_revision", "event_binding_revision", True),
            ("expected_consent_revision", "consent_revision", True),
            ("expected_connection_revision", "connection_revision", True),
            ("event_revision", "event_revision", True),
            ("calendar_list_revision", "calendar_list_revision", True),
            ("goal_id", "goal_id", True),
            ("goal_revision", "goal_revision", True),
            ("purpose", "purpose", True),
        ),
        permissions=("calendar.meeting-prep.v1",),
        schedulable=False,
    ),
}


def get_procedure_template(template_id: str) -> ProcedureV2TemplateSpec:
    spec = PROCEDURE_V2_TEMPLATES.get(str(template_id or "").strip())
    if spec is None:
        raise ProcedureContractError(
            "procedure_template_unknown", "The procedure template is not registered"
        )
    return spec


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def plan_digest(plan: ProcedureV2Plan | Mapping[str, Any]) -> str:
    canonical = plan.model_dump(mode="json") if isinstance(plan, ProcedureV2Plan) else dict(plan)
    return hashlib.sha256(_canonical_json(canonical).encode("utf-8")).hexdigest()


def preview_digest(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(dict(payload)).encode("utf-8")).hexdigest()


def build_procedure_plan(
    template_id: str,
    *,
    step_inputs: Mapping[str, Mapping[str, str]],
) -> ProcedureV2Plan:
    spec = get_procedure_template(template_id)
    steps: list[ProcedureV2Step] = []
    for step in spec.steps:
        source = step_inputs.get(step.step_id)
        if not isinstance(source, Mapping):
            raise ProcedureContractError(
                "procedure_step_input_missing",
                f"Missing immutable input for {step.step_id}",
            )
        steps.append(
            ProcedureV2Step(
                step_id=step.step_id,
                capability_id=step.capability_id,
                capability_version=step.capability_version,
                typed_input_ref=str(source.get("typed_input_ref") or ""),
                typed_input_digest=str(source.get("typed_input_digest") or "").lower(),
            )
        )
    return ProcedureV2Plan(
        schema_version=2,
        template_id=spec.template_id,
        steps=tuple(steps),
        parameters=tuple(
            ProcedureV2Parameter(name=name, kind=kind, required=required)
            for name, kind, required in spec.parameters
        ),
        verifier="leaf_readbacks",
        limits=ProcedureV2Limits(max_steps=2, max_total_seconds=300),
    )


def validate_procedure_plan(plan: Mapping[str, Any] | ProcedureV2Plan) -> ProcedureV2Plan:
    try:
        if isinstance(plan, ProcedureV2Plan):
            return plan
        # Persisted plans cross a JSON boundary.  Pydantic's strict tuple
        # fields intentionally reject the Python lists produced by
        # ``model_dump(mode="json")`` when using ``model_validate`` directly,
        # while ``model_validate_json`` performs the canonical JSON
        # list-to-tuple conversion without relaxing strict scalar validation.
        return ProcedureV2Plan.model_validate_json(_canonical_json(plan))
    except Exception as exc:
        if isinstance(exc, ProcedureContractError):
            raise
        raise ProcedureContractError("procedure_plan_invalid") from exc


def parameter_schema(template_id: str) -> list[dict[str, Any]]:
    spec = get_procedure_template(template_id)
    return [{"name": name, "kind": kind, "required": required} for name, kind, required in spec.parameters]


__all__ = [
    "PROCEDURE_V2_TEMPLATES",
    "ROUTINE_V2_CAPABILITY_VERSION",
    "ROUTINE_V2_STEP_IDS",
    "ProcedureContractError",
    "ProcedureV2Limits",
    "ProcedureV2Parameter",
    "ProcedureV2Plan",
    "ProcedureV2Step",
    "ProcedureV2StepSpec",
    "ProcedureV2TemplateSpec",
    "build_procedure_plan",
    "get_procedure_template",
    "parameter_schema",
    "plan_digest",
    "preview_digest",
    "validate_procedure_plan",
]
