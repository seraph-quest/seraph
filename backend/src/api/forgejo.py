"""Explicit fixed-site controls; configuration/inspection never contact Forgejo."""
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from src.api.work_board import _operator, _owner
from src.browser.forgejo_issue_title import ForgejoError
from src.integrations.forgejo_controls import forgejo_service
from src.security.trust_contract import AuthorityGrant
from src.work_board.contracts import WorkBoardOwner

router = APIRouter(prefix="/capabilities/forgejo", tags=["forgejo"])


class ForgejoOwner(WorkBoardOwner):
    authenticated_token_hash: str | None = Field(default=None, exclude=True, repr=False)


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Configuration(Strict):
    vault_key: str = Field(min_length=1, max_length=256)
    expected_revision: int = Field(ge=0)


class Revision(Strict):
    expected_revision: int = Field(ge=1)


def owner(request):
    operator = _operator(request)
    grants = {getattr(value, "value", value) for value in operator.principal.grants}
    if AuthorityGrant.CAPABILITY_EXECUTE.value not in grants:
        raise HTTPException(403, detail={"code": "forgejo_current_operator_grant_required"})
    bound = _owner(operator)
    return ForgejoOwner(principal_id=bound.principal_id, session_id=bound.session_id,
                       authenticated_token_hash=getattr(operator, "_token_hash", None))


async def response(call):
    try: return await call
    except ForgejoError as exc:
        raise HTTPException(exc.status_code, detail={"code": exc.reason}) from None


@router.get("/connection")
async def connection(request: Request):
    return await response(forgejo_service.connection(owner(request)))


@router.put("/connection")
async def configure(request: Request, body: Configuration):
    return await response(forgejo_service.configure_from_vault(owner(request), **body.model_dump()))


@router.post("/connection/revoke")
async def revoke(request: Request, body: Revision):
    return await response(forgejo_service.revoke(owner(request), **body.model_dump()))
