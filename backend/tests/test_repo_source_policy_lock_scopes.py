"""Original Source lock lifetimes through the authenticated callback journey.

Transparent observers delegate to original Source, SQL and model owners. The
existing journey supplies authenticated admission, real policy/accounting and
its confined scripted HTTP boundary; no test issues a Source, ticket or grant.
Prepared only: requires Root's independent review before execution.
"""
import asyncio
from contextvars import ContextVar
from dataclasses import replace
from threading import Event

import pytest
from sqlalchemy import select

from src.db.models import ApprovalRequest, InferenceCostReservation, RepoRepairProposal
from src.llm_runtime import FallbackLiteLLMModel
from src.model_fabric.configuration import (
    read_model_fabric_configuration, write_model_fabric_configuration,
)
from src.model_fabric import effective_policy
from src.workflows import repo_repair_source as source
from src.workflows.job_runtime import DurableJobLeaseError
from src.workflows.repo_repair import RepoRepairService
from tests.repository_admission_lifecycle import repository_admission_signer
from tests.test_general_task_planner import accounting_db, forbid_external_inference
from tests.test_repo_work_task_publication import _actual_source_callback_journey


async def _wait_for_provider_or_original_journey(returned, journey):
    """Surface original early failures/results within the same 45-second bound."""
    signal = asyncio.create_task(returned.wait())
    try:
        done, _ = await asyncio.wait({journey, signal}, timeout=45,
            return_when=asyncio.FIRST_COMPLETED)
        if journey in done:
            result = journey.result()  # Preserve its actual exception/traceback.
            raise AssertionError("original journey ended before provider checkpoint: " + repr(result))
        if signal not in done:
            raise TimeoutError("original journey and provider checkpoint still pending after 45 seconds")
        signal.result()
    finally:
        if not signal.done():
            signal.cancel()
        await asyncio.gather(signal, return_exceptions=True)


def _observe_original_scopes(monkeypatch, *, contenders):
    # Resolve the original runtime module owner after the signer fixture
    # installs its canonical lock; never retain a pre-fixture lock alias.
    loop = asyncio.get_running_loop()
    phase = ContextVar("test_repository_policy_phase", default=None)
    returned = asyncio.Event()
    release = Event()
    seen, completed, writes, captured, tasks = [], [], [], {}, []
    provider_scope = [None]
    original_run = source.run_repository_iteration
    original_certify = source.certify_repository_callback_return
    original_precontact = source._repository_precontact
    original_canonical = source.stage_repository_canonical_source
    original_generate = FallbackLiteLLMModel.generate
    original_write = RepoRepairService._write_private_artifact

    async def prove_wait(label):
        if not contenders:
            return
        attempted = asyncio.Event()

        async def mutate():
            attempted.set()
            async with effective_policy.configuration_mutation_lock:
                # Actual canonical publication, preserving the policy bytes
                # and revision so this contention proof does not revoke work.
                current = read_model_fabric_configuration()
                write_model_fabric_configuration(current,
                    expected_revision=current.egress_revision)
                completed.append(label)

        task = asyncio.create_task(mutate())
        tasks.append(task)
        await attempted.wait()
        # The competing task has reached the original lock acquisition.
        assert not task.done(), "policy publication escaped Source's short fence"
        assert label not in completed
        seen.append(label)

    async def run(service, **kwargs):
        captured.update(service=service, jobs=kwargs["jobs"], ticket=kwargs["repository_first_start"])
        token = phase.set("provider")
        try:
            return await original_run(service, **kwargs)
        finally:
            phase.reset(token)

    async def certify(*args, **kwargs):
        token = phase.set("certifier")
        try:
            return await original_certify(*args, **kwargs)
        finally:
            phase.reset(token)

    async def precontact(*args, **kwargs):
        if phase.get() == "provider":
            provider_scope[0] = ("precontact", len(seen))
            await prove_wait(provider_scope[0])
        return await original_precontact(*args, **kwargs)

    async def canonical(*args, **kwargs):
        if contenders and phase.get() == "provider":
            assert provider_scope[0] not in completed
        if contenders and phase.get() == "certifier":
            assert not any(label[0] == "certifier" for label in completed)
        if phase.get() in {"provider", "certifier"}:
            await prove_wait((phase.get(), len(seen)))
        return await original_canonical(*args, **kwargs)

    def generate(model, *args, **kwargs):
        response = original_generate(model, *args, **kwargs)
        if phase.get() == "provider":
            # The real transport/accounting returned, while Source still
            # awaits its actual original model callback. No response is faked.
            loop.call_soon_threadsafe(returned.set)
            assert release.wait(30), "test did not release original provider callback"
        return response

    def write(service, *args, **kwargs):
        if contenders and phase.get() == "provider":
            assert provider_scope[0] not in completed
        if contenders and phase.get() == "certifier":
            assert not any(label[0] == "certifier" for label in completed)
        if phase.get() in {"provider", "certifier"}:
            writes.append((phase.get(), args[0]))
        return original_write(service, *args, **kwargs)

    monkeypatch.setattr(source, "run_repository_iteration", run)
    monkeypatch.setattr(source, "certify_repository_callback_return", certify)
    monkeypatch.setattr(source, "_repository_precontact", precontact)
    monkeypatch.setattr(source, "stage_repository_canonical_source", canonical)
    monkeypatch.setattr(FallbackLiteLLMModel, "generate", generate)
    monkeypatch.setattr(RepoRepairService, "_write_private_artifact", write)
    return returned, release, seen, completed, writes, captured, tasks


@pytest.mark.asyncio
async def test_original_short_scopes_block_policy_publication_but_provider_await_releases_it(
        accounting_db, monkeypatch, repository_admission_signer):
    returned, release, seen, completed, writes, captured, contenders = _observe_original_scopes(
        monkeypatch, contenders=True)
    journey = asyncio.create_task(_actual_source_callback_journey(accounting_db, monkeypatch,
        False, "test_python", stop_at="contacted_wait"))
    try:
        await _wait_for_provider_or_original_journey(returned, journey)
        # A real publication completes while the actual model callback await
        # remains suspended. A provider-wide lock would deadlock here.
        async def publish_during_provider():
            async with effective_policy.configuration_mutation_lock:
                current = read_model_fabric_configuration()
                write_model_fabric_configuration(current,
                    expected_revision=current.egress_revision)
        await asyncio.wait_for(publish_during_provider(), 5)
        assert not journey.done()
        release.set()
        await asyncio.wait_for(journey, 45)
        await asyncio.wait_for(asyncio.gather(*contenders), 5)
        assert len(seen) == 6  # Two real precontacts + all four exact snapshots.
        assert sorted(completed) == sorted(seen)
        assert {label[0] for label in seen} == {"precontact", "provider", "certifier"}
        assert any(phase == "provider" and path.endswith("-response.json") for phase, path in writes)
        assert any(phase == "certifier" and path.endswith("-canonical-source.json") for phase, path in writes)
    finally:
        release.set()
        if not journey.done():
            journey.cancel()
        await asyncio.gather(journey, return_exceptions=True)
        await asyncio.gather(*contenders, return_exceptions=True)


@pytest.mark.asyncio
async def test_original_policy_change_after_transport_denies_before_response_artifact_or_proposal(
        accounting_db, monkeypatch, repository_admission_signer):
    returned, release, seen, completed, writes, captured, contenders = _observe_original_scopes(
        monkeypatch, contenders=False)
    journey = asyncio.create_task(_actual_source_callback_journey(accounting_db, monkeypatch,
        False, "test_python", stop_at="contacted_wait"))
    try:
        await _wait_for_provider_or_original_journey(returned, journey)
        service, jobs, ticket = captured["service"], captured["jobs"], captured["ticket"]
        async with jobs._session() as db:
            costs = list((await db.scalars(select(InferenceCostReservation).where(
                InferenceCostReservation.job_id == ticket.job_id))).all())
            assert len(costs) == 1 and costs[0].state == "settled"
            assert costs[0].contact_started_at is not None
        async def change_current_policy():
            async with effective_policy.configuration_mutation_lock:
                current = read_model_fabric_configuration()
                write_model_fabric_configuration(replace(current,
                    egress_revision=current.egress_revision + 1),
                    expected_revision=current.egress_revision)
        await asyncio.wait_for(change_current_policy(), 5)
        release.set()
        with pytest.raises(DurableJobLeaseError, match="Goal or inference policy changed"):
            await asyncio.wait_for(journey, 45)
        assert writes == []  # Original provider/certifier artifact effects only.
        assert not list(service._workspace().glob("artifacts/repo-repair/model/*-response.json"))
        assert not list(service._workspace().glob("artifacts/repo-repair/patch/*"))
        async with jobs._session() as db:
            assert (await db.scalar(select(RepoRepairProposal).where(
                RepoRepairProposal.workflow_run_id == ticket.job_id))) is None
            assert (await db.scalar(select(ApprovalRequest).where(
                ApprovalRequest.id == "repository-approval:" + ticket.iteration_id))) is None
            run = await jobs._fetch(db, ticket.job_id)
            assert source._repository_record(run, "repository:response:" + ticket.iteration_id) is None
            assert source._repository_record(run, "repository:patch:" + ticket.iteration_id) is None
            assert jobs._repo_repair_reservation_state(run)["status"] == "held"
    finally:
        release.set()
        if not journey.done():
            journey.cancel()
        await asyncio.gather(journey, return_exceptions=True)
        await asyncio.gather(*contenders, return_exceptions=True)
