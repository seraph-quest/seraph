import logging

from fastapi import APIRouter, HTTPException

from src.vault.repository import vault_repository

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/vault/keys")
async def list_vault_keys():
    """List all secret keys with metadata (no values exposed)."""
    return await vault_repository.list_keys()


@router.delete("/vault/keys/{key}")
async def delete_vault_key(key: str):
    """Delete a secret by key."""
    success = await vault_repository.delete(key)
    if not success:
        raise HTTPException(status_code=404, detail="Secret not found")
    return {"status": "ok"}
