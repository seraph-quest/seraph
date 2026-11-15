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
from typing import Any, Mapping, Literal

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


# V3 methods are reviewed data plans, separate from the unchanged fixed v2
# GuardianRoutine/package contract above. Imports of C1 contracts stay local
# because ToolDescriptor embeds the producer declaration defined here.
class _ProcedureV3Closed(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


def procedure_v3_digest(value):
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                     allow_nan=False).encode()
    if len(raw) > 65536:
        raise ValueError("procedure data exceeds 64 KiB")
    return hashlib.sha256(raw).hexdigest()


def _leaf_schema(schema):
    allowed = {"type", "minLength", "maxLength", "minimum", "maximum", "enum", "const", "pattern"}
    if not isinstance(schema, dict) or set(schema) - allowed:
        raise ValueError("closed scalar ordinary input schema required")
    kind = schema.get("type")
    if kind not in {"string", "integer", "boolean", "null"}:
        raise ValueError("scalar ordinary input required")
    finite = [schema["const"]] if "const" in schema else schema.get("enum")
    if finite is not None and (not isinstance(finite, list) or not 1 <= len(finite) <= 32):
        raise ValueError("bounded scalar enumeration required")
    finite_text = finite is not None and all(type(value) is str and len(value) <= 4096 for value in finite)
    finite_int = finite is not None and all(type(value) is int for value in finite)
    if kind == "string" and not finite_text and (type(schema.get("maxLength")) is not int
            or not 0 <= schema["maxLength"] <= 4096):
        raise ValueError("finite ordinary text required")
    if kind == "integer" and not finite_int and (type(schema.get("minimum")) is not int
            or type(schema.get("maximum")) is not int):
        raise ValueError("finite ordinary integer required")
    from jsonschema import Draft202012Validator
    from src.work_board.general_task_schema import validate_safe_patterns
    validate_safe_patterns(schema)
    Draft202012Validator.check_schema(schema)
    return schema


def _pointer_segments(pointer):
    if not isinstance(pointer, str) or not pointer.startswith("/") or len(pointer) > 512:
        raise ValueError("exact bounded input pointer required")
    if re.search(r"~(?![01])", pointer):
        raise ValueError("invalid JSON pointer escape")
    parts = [item.replace("~1", "/").replace("~0", "~") for item in pointer[1:].split("/")]
    if any(not part for part in parts):
        raise ValueError("empty input pointer segment")
    return parts


def procedure_pointer_value(value, pointer):
    for segment in _pointer_segments(pointer):
        if isinstance(value, list):
            if not segment.isdigit() or str(int(segment)) != segment:
                raise ValueError("noncanonical array pointer")
            value = value[int(segment)]
        elif isinstance(value, dict):
            value = value[segment]
        else:
            raise ValueError("pointer does not name an input leaf")
    return value


def _replace_pointer(value, pointer, replacement):
    segments = _pointer_segments(pointer)
    parent = value
    for segment in segments[:-1]:
        parent = parent[int(segment)] if isinstance(parent, list) else parent[segment]
    last = segments[-1]
    if isinstance(parent, list):
        if not last.isdigit() or str(int(last)) != last:
            raise ValueError("noncanonical array pointer")
        parent[int(last)] = replacement
    elif isinstance(parent, dict) and last in parent:
        parent[last] = replacement
    else:
        raise ValueError("parameter target absent")


class ProcedureInputLeafV1(_ProcedureV3Closed):
    input_pointer: str
    kind: Literal["ordinary_fixed", "ordinary_parameter", "typed_dependency", "forbidden"]
    schema: dict[str, Any]

    @model_validator(mode="after")
    def finite_leaf(self):
        _pointer_segments(self.input_pointer)
        if self.kind in {"ordinary_fixed", "ordinary_parameter"}:
            _leaf_schema(self.schema)
        return self


class ProcedureInputContractV1(_ProcedureV3Closed):
    schema_version: Literal["procedure.input.v1"] = "procedure.input.v1"
    producer_id: str = Field(pattern=r"^[A-Za-z0-9_.:-]{1,128}$")
    producer_version: str = Field(min_length=1, max_length=128)
    input_schema_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    classifications: list[ProcedureInputLeafV1] = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def unique_leaves(self):
        pointers = [leaf.input_pointer for leaf in self.classifications]
        if len(set(pointers)) != len(pointers):
            raise ValueError("duplicate producer input classification")
        for pointer in pointers:
            if any(other.startswith(pointer + "/") for other in pointers):
                raise ValueError("overlapping producer input classifications")
        procedure_v3_digest(self.model_dump(mode="json"))
        return self


class ProcedureV3Step(_ProcedureV3Closed):
    step_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    tool_id: str = Field(pattern=r"^[A-Za-z0-9_.:-]{1,128}$")
    input: dict[str, Any]
    depends_on: list[str] = Field(default_factory=list, max_length=15)
    output_contract: dict[str, Any]


class ProcedureV3Parameter(_ProcedureV3Closed):
    name: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    step_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    input_pointer: str
    schema: dict[str, Any]
    producer_id: str = Field(pattern=r"^[A-Za-z0-9_.:-]{1,128}$")
    producer_contract_digest: str = Field(pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def scalar(self):
        _pointer_segments(self.input_pointer)
        _leaf_schema(self.schema)
        return self


class ProcedureV3ToolPin(_ProcedureV3Closed):
    tool_id: str = Field(pattern=r"^[A-Za-z0-9_.:-]{1,128}$")
    version: str = Field(min_length=1, max_length=128)
    input_schema_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    output_schema_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    producer_id: str = Field(pattern=r"^[A-Za-z0-9_.:-]{1,128}$")
    producer_contract_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    effects_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    permissions_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    verifier_digest: str = Field(pattern=r"^[a-f0-9]{64}$")


class ProcedurePlanV3(_ProcedureV3Closed):
    source_task_id: str = Field(min_length=1, max_length=128)
    source_attempt: str = Field(min_length=1, max_length=128)
    steps: list[ProcedureV3Step] = Field(min_length=1, max_length=16)
    parameters: list[ProcedureV3Parameter] = Field(max_length=16)
    tool_contract_versions: list[ProcedureV3ToolPin] = Field(min_length=1, max_length=16)
    output_contract: dict[str, Any]
    permissions_digest: str = Field(pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def complete_plan(self):
        from src.work_board.contracts import PlanSpec
        PlanSpec.model_validate({"revision": 1, "steps": [step.model_dump(mode="json") for step in self.steps]})
        names = [parameter.name for parameter in self.parameters]
        targets = [(parameter.step_id, parameter.input_pointer) for parameter in self.parameters]
        tools = [pin.tool_id for pin in self.tool_contract_versions]
        if len(set(names)) != len(names) or len(set(targets)) != len(targets):
            raise ValueError("duplicate procedure parameter")
        if len(set(tools)) != len(tools) or set(tools) != {step.tool_id for step in self.steps}:
            raise ValueError("exact complete tool pin set required")
        by_id = {step.step_id: step for step in self.steps}
        by_tool = {pin.tool_id: pin for pin in self.tool_contract_versions}
        for parameter in self.parameters:
            pin = by_tool[by_id[parameter.step_id].tool_id]
            if (parameter.producer_id, parameter.producer_contract_digest) != (pin.producer_id, pin.producer_contract_digest):
                raise ValueError("parameter producer differs from original tool pin")
            if procedure_pointer_value(by_id[parameter.step_id].input, parameter.input_pointer) != {"$parameter": parameter.name}:
                raise ValueError("parameter must name its exact template token")
            if any(step == parameter.step_id and pointer.startswith(parameter.input_pointer + "/") for step, pointer in targets):
                raise ValueError("overlapping procedure parameters")
        if self.permissions_digest != procedure_permissions_digest(self.tool_contract_versions):
            raise ValueError("procedure permissions commitment changed")
        procedure_v3_digest(self.model_dump(mode="json"))
        return self


class ProcedureCandidateV3(_ProcedureV3Closed):
    schema_version: Literal["ProcedurePlan.v3"] = "ProcedurePlan.v3"
    plan: ProcedurePlanV3


class ProcedureParameterSelection(_ProcedureV3Closed):
    offer_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    name: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")


class ProcedureSaveRequest(_ProcedureV3Closed):
    expected_revision: int = Field(ge=1)
    source_attempt: str = Field(min_length=1, max_length=128)
    parameter_selections: list[ProcedureParameterSelection] = Field(max_length=16)
    idempotency_key: str = Field(pattern=r"^[A-Za-z0-9_.:-]{1,128}$")


def _validate_parameter(schema, value):
    from jsonschema import Draft202012Validator
    from jsonschema.exceptions import ValidationError
    kind = schema["type"]
    wanted = {"string": str, "integer": int, "boolean": bool, "null": type(None)}[kind]
    if type(value) is not wanted:
        raise ValueError("strict producer parameter type required")
    try:
        Draft202012Validator(schema).validate(value)
    except ValidationError as error:
        raise ValueError("producer parameter violates its exact schema") from error


def instantiate_procedure_plan(plan, parameters):
    """Produce an ordinary C1 plan; substitution conveys no authority."""
    from src.work_board.contracts import PlanSpec
    if set(parameters) != {parameter.name for parameter in plan.parameters}:
        raise ValueError("exact procedure parameter names required")
    steps = json.loads(json.dumps([step.model_dump(mode="json") for step in plan.steps]))
    by_id = {step["step_id"]: step for step in steps}
    for parameter in plan.parameters:
        _validate_parameter(parameter.schema, parameters[parameter.name])
        _replace_pointer(by_id[parameter.step_id]["input"], parameter.input_pointer, parameters[parameter.name])
    from src.work_board.general_task import validate_data
    for step in steps:
        validate_data(step["input"], dependencies=set(step["depends_on"]))
    return PlanSpec.model_validate({"revision": 1, "steps": steps})


def validate_procedure_plan_instance(plan, actual_plan, requested_output):
    """Derive literal values independently, then compare the entire data plan."""
    by_id = {step.step_id: step for step in actual_plan.steps}
    values = {parameter.name: procedure_pointer_value(by_id[parameter.step_id].input,
        parameter.input_pointer) for parameter in plan.parameters}
    expected = instantiate_procedure_plan(plan, values)
    if actual_plan != expected or requested_output != plan.output_contract:
        raise ValueError("complete reviewed procedure plan required")
    return values


def procedure_tool_pin(descriptor):
    contract = descriptor.procedure_inputs
    if contract is None:
        raise ValueError("source_contract_review_required")
    if contract.input_schema_digest != procedure_v3_digest(descriptor.input_schema):
        raise ValueError("producer input schema changed")
    return ProcedureV3ToolPin(tool_id=descriptor.tool_id, version=descriptor.version,
        input_schema_digest=procedure_v3_digest(descriptor.input_schema),
        output_schema_digest=procedure_v3_digest(descriptor.output_schema),
        producer_id=contract.producer_id,
        producer_contract_digest=procedure_v3_digest(contract.model_dump(mode="json")),
        effects_digest=procedure_v3_digest(descriptor.effects),
        permissions_digest=procedure_v3_digest(descriptor.permissions),
        verifier_digest=procedure_v3_digest(descriptor.verifier))


def validate_procedure_tool_contracts(plan, descriptors):
    by_id = {descriptor.tool_id: descriptor for descriptor in descriptors}
    for pin in plan.tool_contract_versions:
        if pin.tool_id not in by_id or procedure_tool_pin(by_id[pin.tool_id]) != pin:
            raise ValueError("procedure registered tool contract changed")
    return True


def procedure_permissions_digest(pins):
    return procedure_v3_digest([{"tool_id": pin.tool_id,
        "permissions_digest": pin.permissions_digest, "effects_digest": pin.effects_digest,
        "producer_contract_digest": pin.producer_contract_digest}
        for pin in sorted(pins, key=lambda item: item.tool_id)])


def classify_procedure_inputs(steps, descriptors):
    """Exhaustive original symbolic input closure, without copied source bodies."""
    from src.work_board.contracts import DependencyPointer
    from src.work_board.general_task import validate_schema, validate_data
    by_tool = {descriptor.tool_id: descriptor for descriptor in descriptors}
    by_step = {step.step_id: step for step in steps}
    offers = []
    for step in steps:
        descriptor = by_tool[step.tool_id]
        pin = procedure_tool_pin(descriptor)
        contract = descriptor.procedure_inputs
        leaves = {leaf.input_pointer: leaf for leaf in contract.classifications}
        validate_data(step.input, dependencies=set(step.depends_on))
        def visit(value, pointer):
            leaf = leaves.get(pointer)
            if isinstance(value, dict) and "$dependency" in value:
                if set(value) != {"$dependency"} or leaf is None or leaf.kind != "typed_dependency":
                    raise ValueError("procedure dependency not producer-classified")
                dependency = DependencyPointer.model_validate(value["$dependency"])
                if dependency.step_id not in step.depends_on:
                    raise ValueError("procedure dependency outside declared DAG")
                schema = by_step[dependency.step_id].output_contract
                if dependency.pointer:
                    for segment in _pointer_segments(dependency.pointer):
                        if schema.get("type") != "object" or segment not in schema.get("required", []):
                            raise ValueError("procedure dependency requires an exact required output field")
                        schema = schema["properties"][segment]
                validate_schema(schema, check_value=False)
                validate_schema(leaf.schema, check_value=False)
                if schema.get("type") != leaf.schema.get("type"):
                    raise ValueError("procedure dependency producer schema mismatch")
                # C1 validates actual resolved values against the exact pinned
                # receiving schema before contact. Do not add a universal
                # schema implication requirement absent from that contract.
                return
            if isinstance(value, (dict, list)):
                if leaf is not None or not value:
                    raise ValueError("procedure input container is unclassified")
                members = value.items() if isinstance(value, dict) else enumerate(value)
                for key, child in members:
                    escaped = str(key).replace("~", "~0").replace("/", "~1")
                    visit(child, pointer + "/" + escaped)
                return
            if leaf is None or leaf.kind not in {"ordinary_fixed", "ordinary_parameter"}:
                raise ValueError("procedure input leaf is forbidden or unclassified")
            _validate_parameter(leaf.schema, value)
            if leaf.kind == "ordinary_parameter":
                offer = {"step_id": step.step_id, "input_pointer": pointer,
                    "schema": leaf.schema, "producer_id": contract.producer_id,
                    "producer_contract_digest": pin.producer_contract_digest}
                offers.append({"offer_id": procedure_v3_digest(offer), **offer})
        visit(step.input, "")
        validate_schema(step.output_contract, check_value=False)
    return offers


def procedure_execution_descriptor_matches(current, original):
    """Legacy execution compatibility does not upgrade original source pins."""
    if current is None or original is None:
        return False
    actual = current.model_dump(mode="json")
    retained = original.model_dump(mode="json")
    if original.procedure_inputs is None:
        actual.pop("procedure_inputs", None)
    return actual == retained
