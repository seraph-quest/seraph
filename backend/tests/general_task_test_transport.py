"""Literal owned HTTP transport, keeping proposal/accounting owners real."""
import json
import httpx

from tests.test_general_task_planner import prepare


async def prepare_literal_planner(sessions, workspace, monkeypatch, owner, plan):
    from config.settings import settings
    monkeypatch.setattr(settings, "openrouter_api_key", "fixture-never-sent")
    from src.workspace.production import ProductionWorkspace, prepare_lifecycle_directory
    monkeypatch.setenv("SERAPH_WORKSPACE_LIFECYCLE_PATH", str(workspace.parent / "planning-deployment-lifecycle"))
    prepare_lifecycle_directory(ProductionWorkspace(host_root=workspace))
    for target in ("src.model_fabric.repository.get_session", "src.api.settings.get_db",
                   "src.work_board.dispatcher.get_session", "src.api.work_board.get_session"):
        monkeypatch.setattr(target, sessions)
    await prepare((workspace, None, sessions), monkeypatch, existing_owner=owner)
    state = {"content": json.dumps(plan.model_dump(mode="json")), "contacts": []}
    class Bytes(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield json.dumps({"id": "gen-functional-proposal", "usage": {"cost": "0"},
                "choices": [{"message": {"role": "assistant", "content": state["content"]}}]}).encode()
    class Boundary(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            assert str(request.url) == "https://openrouter.ai/api/v1/chat/completions"
            state["contacts"].append(json.loads(request.content))
            return httpx.Response(200, request=request, stream=Bytes())
    original_client = httpx.AsyncClient
    def intercepted_client(*args, **kwargs):
        if "transport" not in kwargs:
            kwargs["transport"] = Boundary()
        return original_client(*args, **kwargs)
    monkeypatch.setattr(httpx, "AsyncClient", intercepted_client)
    from src.work_board.general_task_planner import GeneralTaskPlanner
    return GeneralTaskPlanner(), state
