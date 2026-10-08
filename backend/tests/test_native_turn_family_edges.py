"""Actual original producer mutation boundaries; final model transport scripted."""
import pytest
from httpx import ASGITransport, AsyncClient

from config.settings import settings
from smolagents import ToolCallingAgent
from tests.test_native_turn_transport import ControlledScriptedModel, ScriptedModel, composition_db, native_transport


@pytest.mark.asyncio
@pytest.mark.parametrize("timing", ["before_prepare", "inside_original_model"])
@pytest.mark.parametrize("leaf", ["final_answer", "clarify"])
async def test_actual_native_final_leaf_mutation_denies_before_effect(native_transport, monkeypatch, timing, leaf):
    from src.app import create_app
    from src.tools.clarify_tool import clarify
    effects = []
    base = ScriptedModel if leaf == "final_answer" else ControlledScriptedModel
    class LeafMutationModel(base):
        def generate(self, messages, **kwargs):
            if timing == "inside_original_model":
                replace_leaf()
            return super().generate(messages, **kwargs)
    model = LeafMutationModel() if leaf == "final_answer" else LeafMutationModel("clarify")
    agent = ToolCallingAgent(tools=[] if leaf == "final_answer" else [clarify], model=model, max_steps=1, verbosity_level=0)
    def replace_leaf():
        def foreign_forward(*args, **kwargs):
            effects.append("unsupported leaf contact")
            return "Mutated reply"
        monkeypatch.setattr(agent.tools[leaf], "forward", foreign_forward)
    if timing == "before_prepare":
        replace_leaf()
    monkeypatch.setattr("src.api.chat.create_onboarding_agent", lambda message: agent)
    async with AsyncClient(transport=ASGITransport(app=create_app()), base_url="http://127.0.0.1:8004",
        cookies={settings.operator_auth_cookie_name: native_transport[1]}, headers={"origin": "http://127.0.0.1:3001"}) as client:
        response = await client.post("/api/chat", json={"message": "Inspect this short answer",
            "message_id": "owned-leaf-mutation-" + leaf + "-" + timing})
    assert effects == [], (response.status_code, response.text, effects)
    assert response.status_code == 503, response.text
    assert model.calls == (0 if timing == "before_prepare" else 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["foreign_step", "foreign_final_check", "append_during_model", "prepend_during_model", "replace_dispatch", "replace_registry", "replace_dispatch_raise", "replace_registry_raise", "premodel_dispatch", "premodel_registry", "copy_private_callback"])
async def test_actual_native_foreign_callback_denies_before_effect(native_transport, monkeypatch, mode):
    from types import FunctionType
    from smolagents.memory import ActionStep
    from src.app import create_app
    effects = []
    def foreign_step(step, **kwargs):
        effects.append("foreign callback")
    def final_check(answer, memory):
        effects.append("foreign final check")
        return True
    class CallbackMutationModel(ScriptedModel):
        def generate(self, messages, **kwargs):
            if mode == "append_during_model":
                agent.step_callbacks.register(ActionStep, foreign_step)
            if mode == "prepend_during_model":
                agent.step_callbacks._callbacks[ActionStep].insert(0, foreign_step)
            if mode in {"replace_dispatch", "replace_dispatch_raise"}:
                monkeypatch.setattr(agent.step_callbacks, "callback", foreign_step)
            if mode in {"replace_registry", "replace_registry_raise"}:
                from smolagents.memory import CallbackRegistry
                agent.step_callbacks = CallbackRegistry()
                agent.step_callbacks.register(ActionStep, foreign_step)
            if mode in {"replace_dispatch_raise", "replace_registry_raise"}:
                self.calls += 1
                raise RuntimeError("Owned original model failure after registry mutation")
            if mode == "copy_private_callback":
                original = agent.step_callbacks._callbacks[ActionStep][-1]
                agent.step_callbacks._callbacks[ActionStep][-1] = FunctionType(
                    original.__code__, original.__globals__, original.__name__, original.__defaults__, original.__closure__)
            return super().generate(messages, **kwargs)
    model = CallbackMutationModel()
    agent = ToolCallingAgent(tools=[], model=model, max_steps=1, verbosity_level=0,
        step_callbacks=[foreign_step] if mode == "foreign_step" else None,
        final_answer_checks=[final_check] if mode == "foreign_final_check" else None)
    if mode in {"premodel_dispatch", "premodel_registry"}:
        from src.agent.turn_execution import NativeTurnExecution
        from smolagents.memory import CallbackRegistry
        original_prepare = NativeTurnExecution.prepare_agent
        def observe_original_prepare(execution, actual_agent):
            original_prepare(execution, actual_agent)
            original_step = actual_agent._step_stream
            def mutate_before_original_model(memory_step):
                if mode == "premodel_dispatch":
                    actual_agent.step_callbacks.callback = foreign_step
                else:
                    actual_agent.step_callbacks = CallbackRegistry()
                    actual_agent.step_callbacks.register(ActionStep, foreign_step)
                # The actual SDK created this step; no fabricated ActionStep.
                yield from original_step(memory_step)
            monkeypatch.setattr(actual_agent, "_step_stream", mutate_before_original_model)
        monkeypatch.setattr(NativeTurnExecution, "prepare_agent", observe_original_prepare)
    monkeypatch.setattr("src.api.chat.create_onboarding_agent", lambda message: agent)
    async with AsyncClient(transport=ASGITransport(app=create_app()), base_url="http://127.0.0.1:8004",
        cookies={settings.operator_auth_cookie_name: native_transport[1]}, headers={"origin": "http://127.0.0.1:3001"}) as client:
        response = await client.post("/api/chat", json={"message": "Inspect this short answer",
            "message_id": "owned-callback-mutation-" + mode})
    assert effects == [], (response.status_code, response.text, effects)
    assert response.status_code == 503, response.text
    assert model.calls == (0 if mode in {"foreign_step", "foreign_final_check", "premodel_dispatch", "premodel_registry"} else 1)


@pytest.mark.asyncio
async def test_actual_native_ordinary_model_exception_retains_original_sdk_cause(native_transport, monkeypatch):
    from src.app import create_app
    from smolagents.utils import AgentGenerationError
    from src.agent.turn_execution import NativeTurnExecution
    observed_errors = []
    original_execute = NativeTurnExecution.execute
    async def observe_original_execute(execution, awaitable):
        try:
            return await original_execute(execution, awaitable)
        except AgentGenerationError as error:
            observed_errors.append(error)
            raise
    monkeypatch.setattr(NativeTurnExecution, "execute", observe_original_execute)
    original_error = RuntimeError("Owned ordinary original model failure")
    class OrdinaryFailureModel(ScriptedModel):
        def generate(self, messages, **kwargs):
            self.calls += 1
            raise original_error
    model = OrdinaryFailureModel()
    agent = ToolCallingAgent(tools=[], model=model, max_steps=1, verbosity_level=0)
    monkeypatch.setattr("src.api.chat.create_onboarding_agent", lambda message: agent)
    async with AsyncClient(transport=ASGITransport(app=create_app()), base_url="http://127.0.0.1:8004",
        cookies={settings.operator_auth_cookie_name: native_transport[1]}, headers={"origin": "http://127.0.0.1:3001"}) as client:
        response = await client.post("/api/chat", json={"message": "Inspect this short answer",
            "message_id": "owned-original-model-error"})
    assert response.status_code == 500, response.text
    assert len(observed_errors) == 1
    assert type(observed_errors[0]) is AgentGenerationError and observed_errors[0].__cause__ is original_error
    assert model.calls == 1
