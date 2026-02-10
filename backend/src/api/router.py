from fastapi import APIRouter

from src.api.chat import router as chat_router
from src.api.goals import router as goals_router
from src.api.profile import router as profile_router
from src.api.sessions import router as sessions_router
from src.api.observer import router as observer_router
from src.api.settings import router as settings_router
from src.api.tools import router as tools_router
from src.api.ws import router as ws_router

api_router = APIRouter()

api_router.include_router(chat_router, prefix="/api", tags=["chat"])
api_router.include_router(sessions_router, prefix="/api", tags=["sessions"])
api_router.include_router(goals_router, prefix="/api", tags=["goals"])
api_router.include_router(profile_router, prefix="/api", tags=["profile"])
api_router.include_router(tools_router, prefix="/api", tags=["tools"])
api_router.include_router(observer_router, prefix="/api", tags=["observer"])
api_router.include_router(settings_router, prefix="/api", tags=["settings"])
api_router.include_router(ws_router, prefix="/ws", tags=["websocket"])
