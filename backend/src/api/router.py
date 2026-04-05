from fastapi import APIRouter

from src.api.activity import router as activity_router
from src.api.audit import router as audit_router
from src.api.approvals import router as approvals_router
from src.api.automation import router as automation_router
from src.api.browser import router as browser_router
from src.api.canvas import router as canvas_router
from src.api.catalog import router as catalog_router
from src.api.capabilities import router as capabilities_router
from src.api.chat import router as chat_router
from src.api.extensions import router as extensions_router
from src.api.goals import router as goals_router
from src.api.mcp import router as mcp_router
from src.api.memory import router as memory_router
from src.api.nodes import router as nodes_router
from src.api.operator import router as operator_router
from src.api.profile import router as profile_router
from src.api.sessions import router as sessions_router
from src.api.observer import router as observer_router
from src.api.settings import router as settings_router
from src.api.skills import router as skills_router
from src.api.tools import router as tools_router
from src.api.vault import router as vault_router
from src.api.workflows import router as workflows_router
from src.api.ws import router as ws_router

api_router = APIRouter()

api_router.include_router(activity_router, prefix="/api", tags=["activity"])
api_router.include_router(audit_router, prefix="/api", tags=["audit"])
api_router.include_router(approvals_router, prefix="/api", tags=["approvals"])
api_router.include_router(automation_router, prefix="/api", tags=["automation"])
api_router.include_router(browser_router, prefix="/api", tags=["browser"])
api_router.include_router(canvas_router, prefix="/api", tags=["canvas"])
api_router.include_router(catalog_router, prefix="/api", tags=["catalog"])
api_router.include_router(capabilities_router, prefix="/api", tags=["capabilities"])
api_router.include_router(chat_router, prefix="/api", tags=["chat"])
api_router.include_router(extensions_router, prefix="/api", tags=["extensions"])
api_router.include_router(sessions_router, prefix="/api", tags=["sessions"])
api_router.include_router(goals_router, prefix="/api", tags=["goals"])
api_router.include_router(profile_router, prefix="/api", tags=["profile"])
api_router.include_router(tools_router, prefix="/api", tags=["tools"])
api_router.include_router(mcp_router, prefix="/api", tags=["mcp"])
api_router.include_router(memory_router, prefix="/api", tags=["memory"])
api_router.include_router(nodes_router, prefix="/api", tags=["nodes"])
api_router.include_router(operator_router, prefix="/api", tags=["operator"])
api_router.include_router(skills_router, prefix="/api", tags=["skills"])
api_router.include_router(workflows_router, prefix="/api", tags=["workflows"])
api_router.include_router(observer_router, prefix="/api", tags=["observer"])
api_router.include_router(settings_router, prefix="/api", tags=["settings"])
api_router.include_router(vault_router, prefix="/api", tags=["vault"])
api_router.include_router(ws_router, prefix="/ws", tags=["websocket"])
