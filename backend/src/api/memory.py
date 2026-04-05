from fastapi import APIRouter

from src.memory.providers import list_memory_provider_inventory

router = APIRouter()


@router.get("/memory/providers")
async def list_memory_providers():
    return list_memory_provider_inventory()
