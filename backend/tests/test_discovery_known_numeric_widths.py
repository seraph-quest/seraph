"""Partial known resource widths, never a plan/strategy/admission substitute."""
import ast
from copy import copy
import hashlib
from datetime import timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Annotated, Literal
from uuid import NAMESPACE_URL, UUID, uuid5

import pytest
import pytest_asyncio
from pydantic import Field, PlainSerializer, TypeAdapter, field_validator, model_serializer

from src.db.engine import get_session
from src.guardian.goal_discovery import goal_discovery_service
from src.guardian.goal_programmes import goal_programme_service
from src.guardian import research_plan_contracts as contracts
from src.memory.header_bounds import (
    HeaderReadBudget, HeaderBoundsError, MAX_BYTES, MAX_ROWS,
    _discovery_strategy_independent_trace, _trace_memory_numeric_charges,
)
from src.memory.composition_headers import _certify_current_memory_snapshot_on_connection
from src.runtime_plugins.bridge import cordis_host
from src.runtime_plugins.ownership import begin_native_writer, bind_invocation
from src.workflows.job_runtime import _canonical, _discovery_strategy_independent_widths
from tests.test_auth_session_composition_privacy import sql_state
from tests.test_programme_prospective_live_selection import (
    genuine_live_selection, private_outputs, SelectionObserved,
)


pytestmark = [pytest.mark.asyncio, pytest.mark.parametrize("scenario", ["positive"])]


def _bytes(value):
    return len(_canonical(value).encode("utf-8"))


@pytest_asyncio.fixture
async def numeric_inputs(genuine_live_selection, monkeypatch):
    """Capture original resource facts; unwind without returning authority."""
    case = genuine_live_selection
    before, files = sql_state(case), private_outputs(case)
    captured = {}

    async def boundary(**requested):
        assert requested["goal_id"] == case.goal_id
        budget = HeaderReadBudget()
        async with get_session(header_budget=budget) as db:
            guard = await begin_native_writer(db, owner="finite_service", header_budget=budget)
            connection = await db.connection()
            with _trace_memory_numeric_charges(budget) as trace:
                common = await connection.run_sync(lambda conn:
                    _certify_current_memory_snapshot_on_connection(conn, budget))
                programme, binding, _common, _identity = await connection.run_sync(lambda conn:
                    guard._select_programme_admission(conn, common,
                        service=goal_discovery_service, host=cordis_host,
                        goal_id=case.goal_id, programme_id=case.programme["id"],
                        grant_revision=case.programme["grant_revision"]))
                native = await bind_invocation(db, method="research.executeAccepted",
                    native_branch="public_research", goal_bound=True, programme_bound=True,
                    reviewed_composition=cordis_host.reviewed, header_budget=budget)
            now = goal_programme_service._clock().astimezone(timezone.utc)
            identifier = uuid5(NAMESPACE_URL,
                f"seraph:public-discovery:{binding.owner_identity_id}:{programme.id}:{now.date().isoformat()}")
            captured.update(programme=programme, binding=binding, identifier=identifier, now=now,
                deadline=min(programme.expires_at, now + timedelta(seconds=300)),
                composition_binding=native, trace=tuple(trace), budget=budget)
            raise SelectionObserved("known numeric inputs captured; no authority returned")

    def no_strategy(*args, **kwargs):
        pytest.fail("Known numeric helper tried to resolve a strategy")

    monkeypatch.setattr(goal_programme_service, "assert_authority", boundary)
    monkeypatch.setattr(goal_discovery_service, "_strategy", no_strategy)
    with pytest.raises(SelectionObserved, match="^known numeric inputs captured; no authority returned$"):
        await goal_discovery_service.admit(goal_id=case.goal_id,
            programme_id=case.programme["id"], grant_revision=case.programme["grant_revision"])
    assert sql_state(case) == before and private_outputs(case) == files
    assert case.contacts == [] and goal_discovery_service._tasks == set()
    yield SimpleNamespace(**captured)
    assert sql_state(case) == before and private_outputs(case) == files
    assert case.contacts == [] and goal_discovery_service._tasks == set()


def _arguments(case):
    return {name: getattr(case, name) for name in (
        "programme", "binding", "identifier", "now", "deadline", "composition_binding")}


def _budget_state(case):
    return case.budget.remaining, set(case.budget.references), set(case.budget.future_references)


async def test_known_plan_fragment_uses_original_nested_serializers(numeric_inputs, scenario):
    case = numeric_inputs
    before = _budget_state(case)
    result = _discovery_strategy_independent_widths(**_arguments(case))
    assert result["complete"] is False
    assert result["plan_bytes"] is None and result["row_header_bytes"] is None
    assert {"strategy_binding", "plan_material", "checkpoint_material", "row_header",
        "strategy_traversals", "caller_occurrence_schedule"}.issubset(result["unresolved_widths"])
    assert result["known_widths"]["brief_content"] == len(case.programme.public_brief.encode())
    assert result["known_widths"]["composition_binding"] == len(case.composition_binding.to_json().encode())

    limits = contracts.ResearchLimits(max_queries=3, max_results=15, max_sources=4,
        max_inference_requests=4, max_wall_seconds=300, max_search_seconds=20,
        max_search_bytes=524288, max_source_bytes=262144, max_output_bytes=65536,
        cost_limit_microusd=case.programme.budget.max_inference_microusd)
    sources = {"search_public": [("plan_queries", "queries")],
        "extract_sources": [("search_public", "manifest"), ("search_public", "selection")],
        "prepare_brief": [("extract_sources", "snapshots")]}
    caps = {"manifest": 65536, "selection": 8192, "snapshots": 65536, "brief": 65536}
    # Real nested data serializers only. The unresolved first ArtifactRef and
    # strategy are not constructed; no ResearchPlan instance is fabricated.
    steps = [contracts.ResearchStep(step_id=name, capability_id=capability, capability_version=1,
        input_refs=[contracts.OutputRef(producer_step_id=producer, output_slot=slot, json_pointer="")
            for producer, slot in sources[name]],
        output_slots=[contracts.OutputSlot(slot=slot, artifact_type=kind, max_bytes=caps[slot])
            for slot, kind in outputs]) for name, capability, outputs in contracts.STAGES[1:]]
    fragment = {"schema_version": 1, "plan_id": case.identifier,
        "programme_id": UUID(case.programme.id), "programme_revision": case.programme.grant_revision,
        "goal_id": case.binding.goal_id, "goal_revision": case.binding.goal_revision,
        "grant_id": case.programme.id, "grant_revision": case.programme.grant_revision,
        "public_brief_digest": case.programme.brief_digest, "route_epoch": case.programme.route_epoch,
        "issued_at": case.now, "deadline_at": case.deadline, "idempotency_key": case.identifier,
        "limits": limits, "steps": steps}
    serialized = {key: TypeAdapter(contracts.GoalResearchPlanSpecV1.model_fields[key].annotation)
        .dump_python(value, mode="json") for key, value in fragment.items()}
    assert serialized["steps"] == [step.model_dump(mode="json") for step in steps]
    assert serialized["limits"] == limits.model_dump(mode="json")
    assert serialized["issued_at"].endswith("Z") and serialized["deadline_at"].endswith("Z")
    # Close the first step's known fields with their ORIGINAL serializers,
    # keeping its absent future ArtifactRef purely as a scalar grammar width.
    first_name, first_capability, first_slots = contracts.STAGES[0]
    first = {"step_id": first_name, "capability_id": first_capability,
        "capability_version": 1, "output_slots": [contracts.OutputSlot(
            slot=slot, artifact_type=kind, max_bytes=16384) for slot, kind in first_slots]}
    first_serialized = {key: TypeAdapter(contracts.ResearchStep.model_fields[key].annotation)
        .dump_python(value, mode="json") for key, value in first.items()}
    serialized["steps"].insert(0, first_serialized)
    # Original content-address grammar: quoted 28-char artifact ID, quoted
    # 64-char digest and integer version. No reference object is manufactured.
    reference_width = 2 + sum(_bytes(key) + 1 + width for key, width in (
        ("artifact_id", 30), ("digest", 66), ("schema_version", 1))) + 2
    assert result["known_widths"]["reference_json"] == reference_width
    first_ref_field = 1 + _bytes("input_refs") + 1 + 2 + reference_width
    strategy_key = 1 + _bytes("strategy_binding") + 1
    expected_known = _bytes(serialized) + first_ref_field + strategy_key
    # Pydantic emits UTC Z while the original constructor material bound uses
    # +00:00: exactly five extra bytes for each of the two UTC timestamps.
    assert result["known_widths"]["plan_fixed_material"] == expected_known + 10
    # No complete plan/ref/row-byte oracle or shared-frame-fit claim follows.
    assert _budget_state(case) == before


@pytest.mark.parametrize("model_name", ["OutputRef", "OutputSlot", "ResearchStep", "ResearchLimits"])
async def test_nested_added_serialized_field_fails_closed(numeric_inputs, monkeypatch, scenario, model_name):
    original = getattr(contracts, model_name)
    changed = type("ChangedNestedModel", (original,),
        {"__annotations__": {"new_serialized_field": str}, "new_serialized_field": "unexpected"})
    monkeypatch.setattr(contracts, model_name, changed)
    with pytest.raises(HeaderBoundsError, match="^programme_constructor_schema_changed$"):
        _discovery_strategy_independent_widths(**_arguments(numeric_inputs))


@pytest.mark.parametrize("damage", ["default", "alias", "serializer"])
async def test_nested_serializer_or_default_drift_fails_closed(numeric_inputs, monkeypatch, scenario, damage):
    if damage == "default":
        class Changed(contracts.OutputRef):
            json_pointer: str = ""
    elif damage == "alias":
        class Changed(contracts.OutputRef):
            json_pointer: str = Field(alias="pointer")
    else:
        class Changed(contracts.OutputRef):
            @model_serializer(mode="wrap")
            def extra_serialized_bytes(self, handler):
                return {**handler(self), "extra": "unexpected"}
    monkeypatch.setattr(contracts, "OutputRef", Changed)
    with pytest.raises(HeaderBoundsError, match="^programme_constructor_schema_changed$"):
        _discovery_strategy_independent_widths(**_arguments(numeric_inputs))


@pytest.mark.parametrize("damage", ["nested_float", "reference_float", "reference_default", "top_serializer"])
async def test_exact_field_type_and_top_serializer_drift_fails_closed(numeric_inputs, monkeypatch, scenario, damage):
    # Closed field names and inherited validators alone do not pin JSON widths.
    # No changed model is instantiated: this is schema drift, not a future ref.
    if damage == "nested_float":
        class Changed(contracts.ResearchStep):
            capability_version: float
        model_name = "ResearchStep"
    elif damage == "reference_float":
        class Changed(contracts.ArtifactRef):
            schema_version: float
        model_name = "ArtifactRef"
    elif damage == "reference_default":
        class Changed(contracts.ArtifactRef):
            schema_version: Literal[1] = 1
        model_name = "ArtifactRef"
    else:
        class Changed(contracts.GoalResearchPlanSpecV1):
            plan_id: Annotated[UUID, PlainSerializer(lambda value: "expanded:" + str(value), return_type=str)]
        model_name = "GoalResearchPlanSpecV1"
    assert set(Changed.model_fields) == set(getattr(contracts, model_name).model_fields)
    before = _budget_state(numeric_inputs)
    monkeypatch.setattr(contracts, model_name, Changed)
    with pytest.raises(HeaderBoundsError, match="^programme_constructor_schema_changed$"):
        _discovery_strategy_independent_widths(**_arguments(numeric_inputs))
    assert _budget_state(numeric_inputs) == before


@pytest.mark.parametrize("damage", ["field", "mode"])
async def test_same_named_validator_registration_drift_fails_closed(numeric_inputs, monkeypatch, scenario, damage):
    target = "output_slot" if damage == "field" else "json_pointer"
    mode = "before" if damage == "mode" else "after"

    class Changed(contracts.OutputRef):
        @field_validator(target, mode=mode)
        @classmethod
        def finite_pointer(cls, value):
            if value != "":
                raise ValueError("fixed research stages consume whole outputs; JSON pointers are unsupported")
            return value

    original = contracts.OutputRef.__pydantic_decorators__.field_validators
    changed = Changed.__pydantic_decorators__.field_validators
    assert set(changed) == set(original) == {"finite_pointer"}
    assert changed["finite_pointer"].info.fields == (target,)
    assert changed["finite_pointer"].info.mode == mode
    # Identical names/body and unchanged fields do not pin registration scope.
    # Validator body semantics remain a separate source-review responsibility.
    before = _budget_state(numeric_inputs)
    monkeypatch.setattr(contracts, "OutputRef", Changed)
    with pytest.raises(HeaderBoundsError, match="^programme_constructor_schema_changed$"):
        _discovery_strategy_independent_widths(**_arguments(numeric_inputs))
    assert _budget_state(numeric_inputs) == before


@pytest.mark.parametrize("damage", ["pattern", "constraint_type"])
async def test_exact_constraint_metadata_drift_fails_closed(numeric_inputs, monkeypatch, scenario, damage):
    original = contracts.ArtifactRef.model_fields["artifact_id"]

    class Changed(contracts.ArtifactRef):
        pass

    field = Changed.model_fields["artifact_id"]
    assert field is not original
    field.metadata = list(field.metadata)
    attribute = "pattern" if damage == "pattern" else "max_length"
    ordinal = next(i for i, marker in enumerate(field.metadata) if hasattr(marker, attribute))
    marker = copy(field.metadata[ordinal])
    previous = getattr(marker, attribute)
    replacement = r"^[A-Za-z0-9:_]+$" if damage == "pattern" else float(previous)
    # Change a copied schema constraint, never a model or accepted reference.
    # object.__setattr__ permits deliberate negative drift of frozen MaxLen.
    object.__setattr__(marker, attribute, replacement)
    field.metadata[ordinal] = marker
    assert getattr(original.metadata[ordinal], attribute) == previous
    if damage == "constraint_type":
        assert type(previous) is int and type(replacement) is float
        assert previous == replacement  # Equality alone misses strict type drift.
    else:
        assert previous != replacement
    before = _budget_state(numeric_inputs)
    monkeypatch.setattr(contracts, "ArtifactRef", Changed)
    with pytest.raises(HeaderBoundsError, match="^programme_constructor_schema_changed$"):
        _discovery_strategy_independent_widths(**_arguments(numeric_inputs))
    assert _budget_state(numeric_inputs) == before


@pytest.mark.parametrize("damage", ["naive_now", "naive_deadline", "offset_now", "offset_deadline",
    "too_long", "shortened"])
async def test_time_inputs_match_original_utc_fixed_deadline(numeric_inputs, scenario, damage):
    kwargs = _arguments(numeric_inputs)
    if damage.startswith("naive_"):
        field = damage.removeprefix("naive_")
        kwargs[field] = kwargs[field].replace(tzinfo=None)
    elif damage.startswith("offset_"):
        field = damage.removeprefix("offset_")
        kwargs[field] = kwargs[field].astimezone(timezone(timedelta(hours=1)))
    elif damage == "too_long":
        kwargs["deadline"] = kwargs["now"] + timedelta(seconds=301)
    else:
        kwargs["deadline"] -= timedelta(seconds=1)
    with pytest.raises(HeaderBoundsError, match="^programme_constructor_numeric_inputs_changed$"):
        _discovery_strategy_independent_widths(**kwargs)


async def test_real_trace_preserves_repeated_ordinals_without_payment(numeric_inputs, scenario):
    case = numeric_inputs
    before = _budget_state(case)
    locator = next(item for item in case.trace if isinstance(item[0], tuple)
        and item[0][:2] == ("locator-metadata", "workflow_run_states"))
    headers = next(item for item in case.trace if isinstance(item[0], tuple)
        and item[0][:2] == ("complete-headers", "workflow_run_states"))
    repeated = (locator, headers, locator, headers)
    result = _discovery_strategy_independent_trace(repeated,
        job_id="goal-discovery:" + case.identifier.hex)
    assert result["original_occurrences"] == repeated
    assert [item[0] for item in result["known_additions"]] == [0, 2]
    assert result["known_additions"][0][2] == result["known_additions"][1][2] > 0
    from src.memory.composition_headers import _metadata_cost
    address = "goal-discovery:" + case.identifier.hex
    assert result["known_additions"][0][2] == _metadata_cost(
        [[2**63 - 1, "text", len(address.encode()), address, None]])
    assert result["unresolved_occurrences"] == (
        (1, "complete-headers", "future-row-header-bytes"),
        (3, "complete-headers", "future-row-header-bytes"))
    assert result["complete"] is False
    assert "future-row-header-bytes" in result["unresolved"]
    assert _budget_state(case) == before


@pytest.mark.parametrize("damage", ["bool_amount", "negative_amount", "oversized_amount",
    "locator_shape", "duplicate", "candidate_present", "local_reference_ceiling", "job_id"])
async def test_partial_trace_bad_metadata_is_rejected(numeric_inputs, scenario, damage):
    case = numeric_inputs
    original = next(item for item in case.trace if isinstance(item[0], tuple)
        and item[0][:2] == ("locator-metadata", "workflow_run_states"))
    appearance, amount = original
    job_id = "goal-discovery:" + case.identifier.hex
    if damage in {"bool_amount", "negative_amount", "oversized_amount"}:
        amount = {"bool_amount": True, "negative_amount": -1, "oversized_amount": MAX_BYTES + 1}[damage]
    elif damage == "locator_shape":
        appearance = (*appearance[:3], True, appearance[4])
    elif damage == "duplicate":
        appearance = (*appearance[:4], (case.binding.goal_id, case.binding.goal_id))
    elif damage == "candidate_present":
        appearance = (*appearance[:4], (job_id,))
    elif damage == "local_reference_ceiling":
        # Negative numeric input only; these addresses are never SQL rows or
        # an accepted capacity fixture. This checks this one locator group.
        appearance = (*appearance[:4], tuple(f"negative-address-{i}" for i in range(MAX_ROWS)))
    else:
        job_id = job_id[:-1] + "G"
    before = _budget_state(case)
    with pytest.raises(HeaderBoundsError, match="^programme_numeric_trace_changed$"):
        _discovery_strategy_independent_trace(((appearance, amount),), job_id=job_id)
    assert _budget_state(case) == before


async def test_original_constructor_and_strategy_callers_remain_unchanged(scenario):
    """Original functions/callers are pinned by the eventual frozen packet."""
    root = Path(__file__).resolve().parents[1]
    assert hashlib.sha256((root / "src/guardian/research_plan_contracts.py").read_bytes()).hexdigest() == (
        "e09eb63459766df01ca6b898fc6d29690453a53018bd1023dae814972a982cb8")
    assert hashlib.sha256((root / "src/guardian/goal_discovery.py").read_bytes()).hexdigest() == (
        "568fad0690ce3fb3b0978919e32bbbb95c223a303be659695e15a3ca9093f27b")
    source = (root / "src/workflows/job_runtime.py").read_text()
    tree = ast.parse(source)
    known = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
        and node.name == "_discovery_strategy_independent_widths")
    shapes = next(node.value for node in known.body if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "shapes" for target in node.targets))
    plan_shape = [item for item in shapes.elts if isinstance(item, ast.Tuple)
        and isinstance(item.elts[0], ast.Name) and item.elts[0].id == "GoalResearchPlanSpecV1"]
    assert len(plan_shape) == 1
    expected_fields = plan_shape[0].elts[1]
    schema_slot = [value for key, value in zip(expected_fields.keys, expected_fields.values)
        if isinstance(key, ast.Constant) and key.value == "strategy_binding"]
    assert len(schema_slot) == 1
    assert isinstance(schema_slot[0], ast.Name) and schema_slot[0].id == "TaskStrategyBinding"
    # The class symbol may pin this original schema annotation, but nowhere
    # else: no constructor, body value, model validation or resolver access.
    strategy_names = [node for node in ast.walk(known) if isinstance(node, ast.Name)
        and node.id == "TaskStrategyBinding"]
    assert len(strategy_names) == 1 and strategy_names[0] is schema_slot[0]
    assert not any(isinstance(node, ast.Attribute) and node.attr == "strategy_binding"
        for node in ast.walk(known))
    assert not any(isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        and node.func.id in {"getattr", "hasattr"}
        and len(node.args) >= 2 and isinstance(node.args[1], ast.Constant)
        and node.args[1].value == "strategy_binding" for node in ast.walk(known))
    assert not any(isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        and node.func.attr in {"_strategy", "resolve_task_strategy", "model_construct",
            "model_validate", "model_validate_json", "validate_python", "validate_json"}
        for node in ast.walk(known))
    originals = {"_discovery_constructor_numeric_bounds":
        "a2139ba049ebcccac13ff91925efb948f412315e80c2e5ef0ee13b628f92b193",
        "_validate_discovery_constructor_numeric_bounds":
        "923e70b6f801411e231dcdc4c701d8150e678a4f8dd6b55115326f09773c1aff"}
    for name, expected in originals.items():
        function = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == name)
        assert not function.decorator_list
        original_source = ast.get_source_segment(source, function)
        assert original_source is not None
        assert hashlib.sha256(original_source.encode()).hexdigest() == expected
