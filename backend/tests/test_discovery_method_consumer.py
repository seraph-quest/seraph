"""Consumer protocol/projection checks, not signed method adoption proof."""
from types import SimpleNamespace

import pytest

from src.guardian.goal_discovery import GoalDiscoveryService
from src.work_board.contracts import TaskStrategyBinding
from src.workflows.job_runtime import _digest
from src.workflows.research_provider import discovery_strategy_inputs


def active_strategy():
    from src.memory.task_lessons import ResearchStrategy
    data = ResearchStrategy(query_templates=["official dated release"],
        source_preferences=["official", "dated"],
        required_evidence_fields=["url", "date", "excerpt", "limitation"],
        draft_sections=["Evidence", "Limitations"],
        stop_conditions=["Stop without attributed evidence"]).model_dump(mode="json")
    return TaskStrategyBinding(status="active", method_id="original-proposal",
        version="original-memory", digest=_digest(data), typed_data=data)


@pytest.mark.asyncio
@pytest.mark.parametrize("later_pointer", ["changed", "rolled_back", "blocked", "unavailable"])
async def test_reopen_uses_exact_original_pin_without_current_resolution(monkeypatch, later_pointer):
    # The canonical owner will authenticate these versions. Here the protocol
    # double only proves that a consumer never asks for a new selection.
    original = active_strategy()
    grant = SimpleNamespace(goal_id="original-goal")
    db = object()
    calls = []

    async def no_secret(text):
        return text
    monkeypatch.setattr("src.memory.m5.sanitize_m5_memory_text_async", no_secret)

    class Resolver:
        def resolve(self, *_args, **_kwargs):
            raise AssertionError("later current selection must not run: " + later_pointer)

        async def validate_pinned(self, owner, goal_ref, binding, programme_grant=None, db=None):
            assert owner.principal_id == "service:guardian-goal-programmes" and owner.session_id == ""
            assert goal_ref == grant.goal_id and programme_grant is grant
            assert binding == original
            calls.append(db)
            return binding

    service = GoalDiscoveryService(strategy_resolver=Resolver())
    await service.start()
    try:
        for _ in range(3):
            assert await service._validate_pinned_strategy(grant, original, db=db) == original
    finally:
        await service.stop()
    assert calls == [db, db, db]


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["method_revoked", "method_tombstoned", "goal_stale", "programme_expired"])
async def test_canonical_pin_denial_propagates_before_strategy_projection(monkeypatch, reason):
    original = active_strategy()
    contacts = []

    async def denied(*_args, **_kwargs):
        raise ValueError(reason)

    async def forbidden_projection(*_args, **_kwargs):
        contacts.append("projection")
        raise AssertionError("denied pin must not reach projection")
    monkeypatch.setattr("src.guardian.goal_discovery.validated_discovery_strategy", forbidden_projection)
    service = GoalDiscoveryService(strategy_resolver=SimpleNamespace(validate_pinned=denied))
    await service.start()
    try:
        with pytest.raises(ValueError, match=reason):
            await service._validate_pinned_strategy(SimpleNamespace(goal_id="goal"), original)
    finally:
        await service.stop()
    assert contacts == []


@pytest.mark.asyncio
async def test_validator_cannot_replace_original_version_or_fallback_to_resolve():
    original = active_strategy()

    async def replace(*_args, **_kwargs):
        return original.model_copy(update={"version": "different-memory"})

    service = GoalDiscoveryService(strategy_resolver=SimpleNamespace(validate_pinned=replace))
    await service.start()
    try:
        with pytest.raises(ValueError, match="accepted strategy binding changed"):
            await service._validate_pinned_strategy(SimpleNamespace(goal_id="goal"), original)
        service.strategy_resolver = SimpleNamespace(resolve=lambda *_args: original)
        with pytest.raises(ValueError, match="pinned_strategy_validator_unavailable"):
            await service._validate_pinned_strategy(SimpleNamespace(goal_id="goal"), original)
        service.strategy_resolver = None
        with pytest.raises(ValueError, match="pinned_strategy_validator_unavailable"):
            await service._validate_pinned_strategy(SimpleNamespace(goal_id="goal"), original)
    finally:
        await service.stop()


@pytest.mark.asyncio
async def test_all_three_projection_boundaries_keep_original_reference_and_exact_fields(monkeypatch):
    async def no_secret(text):
        return text
    monkeypatch.setattr("src.memory.m5.sanitize_m5_memory_text_async", no_secret)
    original = active_strategy()
    reference = {"status": "active", "method_id": original.method_id,
        "version": original.version, "digest": original.digest}
    fields = [("query_templates",), ("source_preferences", "required_evidence_fields"),
        ("draft_sections", "required_evidence_fields", "stop_conditions")]
    for slot, names in enumerate(fields):
        supplied = await discovery_strategy_inputs(original, slot)
        assert supplied == {"strategy_ref": reference, "research_strategy": {
            "schema_version": "ResearchStrategy.v1",
            **{name: original.typed_data[name] for name in names}}}
    with pytest.raises(ValueError, match="stage_unsupported"):
        await discovery_strategy_inputs(original, 3)
