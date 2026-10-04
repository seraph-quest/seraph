"""Explicit fixed-site controls; configuration/inspection never contact Forgejo."""
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, StrictBool
from typing import Literal

from src.api.work_board import _operator, _owner
from src.browser.forgejo_issue_title import ForgejoError
from src.integrations.forgejo_controls import forgejo_service
from src.security.trust_contract import AuthorityGrant
from src.work_board.contracts import WorkBoardOwner
from src.work_board.repository import BoardError
from src.workflows.job_runtime import DurableJobError

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


class ReadConsent(Revision):
    duration_seconds: int = Field(ge=30,le=900)
    read_ack: StrictBool


class Prepare(Revision):
    operation: Literal["provision","preview","title"]
    fields: dict = Field(default_factory=dict)
    request_key: str = Field(min_length=36,max_length=36)
    goal_id: str = Field(min_length=1,max_length=128)
    goal_revision: int = Field(ge=1)
    preview_job_id: str | None = Field(default=None,max_length=128)
    preview_digest: str | None = Field(default=None,pattern=r"^[a-f0-9]{64}$")


class Execute(Strict):
    expected_revision: int = Field(ge=1)
    fencing_token: int = Field(ge=0)


class Approve(Strict):
    approval_id: str = Field(min_length=1,max_length=128)
    decision: Literal["approved","denied"]
    exact_ack: StrictBool


class Recovery(Revision):
    original_job_revision: int = Field(ge=1)
    original_fencing_token: int = Field(ge=1)
    request_key: str = Field(min_length=36,max_length=36)
    read_ack: StrictBool


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
    except BoardError as exc:
        raise HTTPException(getattr(exc,"status_code",409),detail={"code":exc.code}) from None
    except DurableJobError:
        raise HTTPException(409,detail={"code":"forgejo_durable_job_conflict"}) from None


@router.get("/connection")
async def connection(request: Request):
    return await response(forgejo_service.connection(owner(request)))


@router.put("/connection")
async def configure(request: Request, body: Configuration):
    return await response(forgejo_service.configure_from_vault(owner(request), **body.model_dump()))


@router.post("/connection/revoke")
async def revoke(request: Request, body: Revision):
    return await response(forgejo_service.revoke(owner(request), **body.model_dump()))


@router.put("/connection/read-consent")
async def read_consent(request: Request, body: ReadConsent):
    return await response(forgejo_service.native.consent(owner(request),**body.model_dump()))


@router.post("/jobs")
async def prepare(request: Request, body: Prepare):
    return await response(forgejo_service.native.prepare(owner(request),**body.model_dump()))


@router.get("/jobs/{job_id}")
async def inspect_job(request: Request, job_id: str):
    return await response(forgejo_service.native.snapshot(owner(request),job_id))


@router.get("/jobs/{job_id}/output")
async def output(request: Request, job_id: str):
    return await response(forgejo_service.native.output(owner(request),job_id))


@router.post("/jobs/{job_id}/approve")
async def approve(request: Request, job_id: str, body: Approve):
    return await response(forgejo_service.native.approve(owner(request),job_id,**body.model_dump()))


@router.post("/jobs/{job_id}/execute")
async def execute(request: Request, job_id: str, body: Execute):
    return await response(forgejo_service.native.execute(owner(request),job_id,**body.model_dump()))


@router.post("/jobs/{job_id}/cancel")
async def cancel(request: Request, job_id: str, body: Execute):
    return await response(forgejo_service.native.cancel(owner(request),job_id,**body.model_dump()))


@router.post("/jobs/{job_id}/read-only-recovery")
async def recover(request: Request, job_id: str, body: Recovery):
    return await response(forgejo_service.native.recover(owner(request),job_id,**body.model_dump()))
