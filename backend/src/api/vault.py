import logging

from fastapi import APIRouter, HTTPException, Request

from src.vault.repository import vault_repository

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/vault/keys")
async def list_vault_keys(request: Request):
    """List all secret keys with metadata (no values exposed)."""
    return await vault_repository.list_keys(owner_principal_id=request.state.operator.principal.principal_id)


@router.delete("/vault/keys/{key}")
async def delete_vault_key(key: str, request: Request):
    """Delete a secret by key."""
    success = await vault_repository.delete(key, owner_principal_id=request.state.operator.principal.principal_id)
    if not success:
        raise HTTPException(status_code=404, detail="Secret not found")
    return {"status": "ok"}
