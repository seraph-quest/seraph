"""Fixed source reads under the original authenticated parent authority."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

from sqlalchemy import select

from config.settings import settings
from src.db.models import WorkBoardAttempt, WorkBoardStatus, WorkBoardTask
from src.work_board.contracts import WorkBoardOwner
from src.work_board.research_artifacts import read, normalized_source, write_verified
from src.work_board.research_contracts import PARENT_KIND, ResearchDossierInput, SOURCE_BYTES
from src.workflows.research_native import checkpoint


async def current_inputs(jobs, parent_id):
    from src.auth.service import authenticate_principal, authenticate_session
    from src.model_fabric.effective_policy import current_inference_policy
    from src.work_board.input_artifacts import resolve_input_artifact_for_task
    from src.work_board.pipelines import root_binding
    from src.work_board.pipeline_contracts import digest
    from src.workflows.job_runtime import _assert_canonical_goal_fence
    async with jobs._session() as db:
        parent = await jobs._fetch(db, parent_id)
        authority = json.loads(parent.declared_authority_json)
        operator = await authenticate_session(parent.operator_session_id, touch=False)
        if operator.principal.principal_id != parent.owner_principal_id or operator.session_id != parent.session_id:
            raise ValueError("research requires its exact original active operator Root session")
        await authenticate_principal(parent.owner_principal_id, db=db)
        await _assert_canonical_goal_fence(db, goal_id=parent.goal_id, goal_revision=parent.goal_revision,
            owner_kind=parent.owner_kind, owner_principal_id=parent.owner_principal_id,
            session_id=parent.session_id, authority=parent.declared_authority_json)
        if (parent.job_kind != PARENT_KIND or parent.status not in {"running", "paused"}
            or parent.deadline_at.replace(tzinfo=timezone.utc) <= datetime.now(timezone.utc)
            or authority.get("live_root_digest") != digest(root_binding())
            or authority.get("model_policy_digest") != current_inference_policy()[1]
            or authority.get("source_egress_acknowledged") is not True):
            raise ValueError("original research Root/Goal/source-egress authority changed")
        attempt = await db.scalar(select(WorkBoardAttempt).where(WorkBoardAttempt.workflow_run_id == parent_id))
        task = await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id == attempt.task_id)) if attempt else None
        if (task is None or attempt.ended_at or attempt.cancel_requested_at
            or task.owner_principal_id != parent.owner_principal_id or task.owner_session_id != parent.session_id
            or task.typed_input_digest != authority.get("typed_input_digest")
            or task.input_artifact_id != authority.get("input_artifact_id")):
            raise ValueError("current research Board input/attempt changed")
        resolved = await resolve_input_artifact_for_task(db,
            WorkBoardOwner(principal_id=task.owner_principal_id, session_id=task.owner_session_id),
            artifact_id=task.input_artifact_id, goal_id=task.goal_id, goal_revision=task.goal_revision,
            capability_id=task.capability_id, expected_task_id=task.task_id)
        return ResearchDossierInput.model_validate(resolved.input)


async def _completed_local_source(jobs, *, parent_id, source):
    from src.work_board.input_artifacts import _safe_file_bytes, _open_input_artifact_parent
    from src.work_board.review import _verified_workflow_readback
    from src.workspace import canonical_workspace_root
    async with jobs._session() as db:
        parent = await jobs._fetch(db, parent_id)
        task = await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id == source.producer_task_ref))
        attempt = await db.scalar(select(WorkBoardAttempt).where(WorkBoardAttempt.attempt_id == source.producer_attempt_ref))
        if (task is None or attempt is None or attempt.task_id != task.task_id
            or task.owner_principal_id != parent.owner_principal_id or task.owner_session_id != parent.session_id
            or task.status != WorkBoardStatus.done or attempt.ended_at is None):
            raise ValueError("local source requires the current owner's verified completed task")
        proof = await _verified_workflow_readback(db, task, attempt)
        if not proof or proof.get("content_sha256") != source.source_sha256:
            raise ValueError("local source independent readback digest changed")
        reference = str(proof.get("target_path") or "")
        if not reference.startswith("artifacts/work-board/") or ".." in reference.split("/"):
            raise ValueError("local source is outside the fixed Board artifact directory")
        path = canonical_workspace_root(settings.workspace_dir)/reference
        # Bounded nofollow read validates the physically selected output; a
        # completed label or a caller-supplied path never supplies source bytes.
        import os
        parent_fd, leaf = _open_input_artifact_parent(path, create=False)
        try:
            descriptor = os.open(leaf, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=parent_fd)
            try:
                size = os.fstat(descriptor).st_size
            finally:
                os.close(descriptor)
        finally:
            os.close(parent_fd)
        if not 0 < size <= SOURCE_BYTES:
            raise ValueError("selected local source exceeds 64 KiB")
        raw = _safe_file_bytes(path, expected_digest=source.source_sha256, expected_size=size)
        return raw


async def acquire_source(jobs, *, child_id, owner, fence, source_slot, transport=None):
    """One durable GET intent or exact completed artifact, never a reread."""
    child = await jobs.get_job(child_id)
    parent_id = child["parent_job_id"]
    inputs = await current_inputs(jobs, parent_id)
    source = inputs.sources[source_slot]
    producer_slot = min(slot for slot, perspective in enumerate(inputs.perspectives) if source_slot in perspective.source_slots)
    parent = await jobs.get_job(parent_id)
    creation = checkpoint(parent, "research:creation")
    producer_id = creation["child_ids"][producer_slot]
    producer = await jobs.get_job(producer_id)
    existing = checkpoint(producer, f"research:artifact:source:{source_slot}")
    if existing:
        raw = read(existing["file_path"], existing["content_sha256"], max_bytes=SOURCE_BYTES)
        return normalized_source(raw, source_slot=source_slot, first_line=source.first_line, last_line=source.last_line), existing
    if producer_id != child_id:
        return None
    identifier = f"research:source-intent:{source_slot}"
    if checkpoint(child, identifier):
        raise ValueError("source contact may have happened; exact verified output is required, never a second GET")
    intent = {"creation_digest":creation["creation_digest"], "source_slot":source_slot,
        "selection_digest":hashlib.sha256(json.dumps(source.model_dump(),sort_keys=True).encode()).hexdigest(),
        "kind":source.kind,"no_learning":True}
    await jobs.record_checkpoint(child_id, checkpoint_id=identifier, state=intent,
        checkpoint_payload=intent, owner=owner, fencing_token=fence)
    if source.kind == "completed_board_artifact":
        raw = await _completed_local_source(jobs, parent_id=parent_id, source=source)
    else:
        from src.browser.pinned_transport import PinnedBrowserRequest, PinnedBrowserTransport, parse_public_https_url
        parsed = parse_public_https_url(source.url)
        selected = transport or PinnedBrowserTransport(timeout_seconds=20, max_response_bytes=SOURCE_BYTES)
        try:
            remaining = min(20, datetime.fromisoformat(child["deadline_at"]).replace(tzinfo=timezone.utc).timestamp()-datetime.now(timezone.utc).timestamp())
            if remaining <= 0:
                raise TimeoutError("original source deadline expired")
            import asyncio
            async with asyncio.timeout(remaining):
                response = await selected.resolve_and_fetch(PinnedBrowserRequest(url=source.url, method="GET",
                    headers={"accept":"text/plain", "accept-encoding":"identity"},resource_type="document",
                    is_navigation=True,redirect_count=0),allowed_hosts=[parsed.hostname],approved_url_prefixes=[source.url])
            content_type = response.headers.get("content-type", "").lower().replace(" ", "")
            if (response.request_url != source.url or response.status_code != 200 or response.redirect_location or content_type not in {
                "text/plain", "text/plain;charset=utf-8", "text/plain;charset=us-ascii"}):
                raise ValueError("source must be exact nonredirected UTF-8 text/plain")
            raw = response.content
        finally:
            selected.cancel_pending_blocking()
    normalized = normalized_source(raw,source_slot=source_slot,first_line=source.first_line,last_line=source.last_line)
    await current_inputs(jobs,parent_id)
    binding = await write_verified(jobs,job_id=child_id,owner=owner,fence=fence,
        creation_digest=creation["creation_digest"],slot=source_slot,kind="source",content=raw,max_bytes=SOURCE_BYTES)
    return normalized,binding
