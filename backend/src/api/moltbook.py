"""Explicit owner Moltbook controls; metadata GETs never contact the provider."""
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from src.api.work_board import _operator, _owner
from src.integrations.moltbook import MoltbookError
from src.integrations.moltbook_controls import moltbook_service
from src.security.trust_contract import AuthorityGrant
from src.work_board.repository import BoardError
from src.workflows.job_runtime import DurableJobError

router = APIRouter(prefix="/capabilities/moltbook", tags=["moltbook"])


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Setup(Strict):
    vault_key: str = Field(min_length=1, max_length=256)
    request_key: str = Field(min_length=1, max_length=128)
    expected_revision: int | None = Field(default=None, ge=1)


class Consent(Strict):
    request_key: str = Field(min_length=1, max_length=128)
    expected_revision: int = Field(ge=1)
    goal_id: str = Field(min_length=1, max_length=128)
    goal_revision: int = Field(ge=1)
    actions: list[Literal["inspect", "feed", "post", "comments", "community", "create_post", "create_comment"]] = Field(min_length=1, max_length=7)
    duration_seconds: int = Field(ge=30, le=3600)
    personal_noncommercial: Literal[True]
    no_redistribution: Literal[True]


class Revision(Strict):
    expected_revision: int = Field(ge=1)


class Read(Strict):
    operation: Literal["inspect", "feed", "post", "comments", "community"]
    fields: dict = Field(default_factory=dict)
    request_key: str = Field(min_length=1, max_length=128)
    goal_id: str = Field(min_length=1, max_length=128)
    goal_revision: int = Field(ge=1)
    expected_revision: int = Field(ge=1)
    priority: int = Field(default=50, ge=0, le=100)


class Write(Strict):
    operation: Literal["create_post", "create_comment"]
    fields: dict
    request_key: str = Field(min_length=1, max_length=128)
    goal_id: str = Field(min_length=1, max_length=128)
    goal_revision: int = Field(ge=1)
    expected_revision: int = Field(ge=1)
    community_job_id: str = Field(min_length=1, max_length=128)
    community_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    introductions_allowed: Literal[True]
    public_only: Literal[True]
    priority: int = Field(default=50, ge=0, le=100)


class Answer(Strict):
    answer: str = Field(min_length=4, max_length=32)
    request_key: str = Field(min_length=1, max_length=128)


class Decision(Strict):
    approval_id: str = Field(min_length=1, max_length=128)
    decision: Literal["approved", "denied"]


class Cancel(Strict):
    request_key: str = Field(min_length=1, max_length=128)
    expected_revision: int = Field(ge=0)
    fencing_token: int = Field(ge=0)


class Execute(Strict):
    request_key: str = Field(min_length=1, max_length=128)
    expected_phase: Literal["unattempted", "awaiting_create_approval", "awaiting_verify_approval"]
    fencing_token: int = Field(ge=0)


def owner(request, *, contact=False, mutation=False):
    operator = _operator(request)
    grants = {getattr(value, "value", value) for value in operator.principal.grants}
    required = {AuthorityGrant.CAPABILITY_EXECUTE.value}
    # Authenticated Roots intentionally do not carry blanket credential or
    # external-mutation grants. The fixed adapter checks owner Vault identity
    # and finite action-specific consent; writes additionally require an exact
    # independently approved request. Do not broaden the shared Root grants.
    if not required <= grants:
        raise HTTPException(403, detail={"code": "moltbook_current_operator_grant_required"})
    return _owner(operator)


async def response(call):
    try:
        return await call
    except (MoltbookError, BoardError) as exc:
        raise HTTPException(getattr(exc, "status_code", 409), detail={"code": exc.code}) from None
    except DurableJobError:
        raise HTTPException(409, detail={"code": "moltbook_durable_job_conflict"}) from None


@router.get("/connection")
async def connection(request: Request):
    return await response(moltbook_service.connection(owner(request)))


@router.put("/connection")
async def configure(request: Request, body: Setup):
    return await response(moltbook_service.configure_from_vault(owner(request), **body.model_dump()))


@router.post("/connection/consent")
async def consent(request: Request, body: Consent):
    return await response(moltbook_service.consent(owner(request, contact=True,
        mutation=bool({"create_post", "create_comment"} & set(body.actions))), **body.model_dump()))


@router.post("/connection/disable")
async def disable(request: Request, body: Revision):
    return await response(moltbook_service.disable(owner(request), **body.model_dump()))


@router.post("/reads")
async def read(request: Request, body: Read):
    return await response(moltbook_service.prepare_read(owner(request, contact=True), **body.model_dump()))


@router.get("/jobs/{job_id}")
async def job(request: Request, job_id: str):
    return await response(moltbook_service.snapshot(owner(request), job_id))


@router.post("/writes")
async def write(request: Request, body: Write):
    return await response(moltbook_service.prepare_write(owner(request, contact=True, mutation=True), **body.model_dump()))


@router.post("/jobs/{job_id}/approval")
async def approve(request: Request, job_id: str, body: Decision):
    return await response(moltbook_service.approve(owner(request, mutation=True), job_id, **body.model_dump()))


@router.post("/jobs/{job_id}/answer")
async def answer(request: Request, job_id: str, body: Answer):
    return await response(moltbook_service.manual_answer(owner(request, mutation=True), job_id, **body.model_dump()))


@router.post("/jobs/{job_id}/execute")
async def execute(request: Request, job_id: str, body: Execute):
    return await response(moltbook_service.execute(owner(request, contact=True), job_id, **body.model_dump()))


@router.get("/jobs/{job_id}/output")
async def output(request: Request, job_id: str):
    return await response(moltbook_service.output(owner(request), job_id))


@router.post("/jobs/{job_id}/cancel")
async def cancel(request: Request, job_id: str, body: Cancel):
    return await response(moltbook_service.cancel(owner(request), job_id, **body.model_dump()))


@router.post("/jobs/{job_id}/recover")
async def recover(request: Request, job_id: str):
    return await response(moltbook_service.recover(owner(request), job_id))
