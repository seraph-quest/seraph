"""Authenticated existing Work/dispatcher/approval/artifact journey with real Node.

Only the external model transport is intercepted. These reuse the existing
native-repair fixture helpers; no durable authority/broker is replaced.
"""
from __future__ import annotations
import asyncio
from contextlib import suppress
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import time
from types import SimpleNamespace

import pytest
from config.settings import RepoSandboxSettings
from src.execution.repo_node import NodeRepoRepairExecutor
from src.execution import repo_supervisor as supervisor
from src.workflows.repair_capacity import try_acquire_repo_repair_capacity, _QUARANTINED_LANES
from src.workflows.job_runtime import durable_job_repository
from tests import test_repo_repair_local_vertical as native
from tests.test_repo_node import make_fixture, NODE, PROFILE


PROOF_ROOT=Path(os.environ.get("SERAPH_TEST_EVIDENCE_ROOT") or str(Path(__file__).resolve().parents[2]/".agent-evidence/912"))/f"integrated-{os.getpid()}"


def _retained(name):
    PROOF_ROOT.mkdir(mode=0o700,parents=True,exist_ok=True)
    return PROOF_ROOT/name


def _prepare_accounting_fixture(tmp_path,monkeypatch):
    """Use a unique real deployment witness and finite governed reservation."""
    from src.workspace.production import ProductionWorkspace,prepare_lifecycle_directory
    monkeypatch.setenv("SERAPH_WORKSPACE_LIFECYCLE_PATH",str(tmp_path/"deployment-lifecycle"))
    original_path=native.Path
    shared_receipts={"/tmp/seraph-887-native-pending-safe-projection.json","/tmp/seraph-887-local-vertical-receipt.json"}
    def retained_native_path(*args,**kwargs):
        if len(args)==1 and str(args[0]) in shared_receipts:
            return _retained(original_path(args[0]).name)
        return original_path(*args,**kwargs)
    monkeypatch.setattr(native,"Path",retained_native_path)
    original_setup=native.OpenRouterSetup
    monkeypatch.setattr(native,"OpenRouterSetup",lambda **kwargs:original_setup(**kwargs,request_cost_bound_microusd=1_000))
    original_configuration=native._configure_openrouter
    def configure():
        prepare_lifecycle_directory(ProductionWorkspace(host_root=Path(native.settings.workspace_dir)))
        persist=original_configuration()
        async def finish():
            await durable_job_repository.configure_inference_accounting(25_000)
            await persist()
        return finish
    monkeypatch.setattr(native,"_configure_openrouter",configure)


async def _prepare_node_flow(client,async_db,tmp_path,monkeypatch,*,typescript=False,script=None,wall_seconds=180,low_due=False):
    _prepare_accounting_fixture(tmp_path,monkeypatch)
    fixture_root=tmp_path/"fixture";fixture_root.mkdir(mode=0o700)
    executor,fixture_repo,fixture_job=make_fixture(fixture_root,typescript=typescript,script=script)
    request={"repository_path":"repo","problem_statement":"Repair the bounded JS/TS fixture value.","acceptance_criteria":["Actual test and selected build pass."],
             "source_paths":[fixture_job.allowed_paths[0]],"allowed_paths":list(fixture_job.allowed_paths),"test_args":list(fixture_job.test_args)}
    def init_repository(workspace,**kwargs):
        repository=workspace/"repo";shutil.copytree(fixture_repo,repository)
        subprocess.run(["/usr/bin/git","init","--initial-branch=main",str(repository)],check=True,capture_output=True)
        return repository,native._tree_receipt(repository),native._tree_receipt(repository/".git")
    def selected_settings(**kwargs):
        return RepoSandboxSettings(**kwargs,profile=PROFILE,node_runtime_path=NODE,max_wall_seconds=wall_seconds)
    def transport_factory(calls):
        def transport(**kwargs):
            body=kwargs["body"];base=None
            for message in body["messages"]:
                with suppress(ValueError,TypeError):
                    decoded=json.loads(message.get("content",""))
                    if isinstance(decoded,dict) and isinstance(decoded.get("source_packet"),dict):base=decoded["source_packet"]["base_snapshot_sha256"]
            assert isinstance(base,str) and len(base)==64
            calls.append({"endpoint":"https://openrouter.ai/api/v1/chat/completions","runtime_path":kwargs["context"].runtime_path})
            output={"summary":"Fix the approved fixture value.","base_snapshot_sha256":base,"patch_unified_diff":fixture_job.patch_bytes.decode(),"allowed_paths":list(fixture_job.allowed_paths),"test_args":list(fixture_job.test_args),"expected_outcome":"Actual selected tests/build pass."}
            content=json.dumps(output)
            response=SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(role="assistant",content=content))])
            return response,{"id":"gen-node-fixture", "choices":[{"message":{"role":"assistant","content":content}}],"usage":{"prompt_tokens":1,"completion_tokens":1,"total_tokens":2,"cost":"0.000001"}}
        return transport
    monkeypatch.setattr(native,"_init_repository",init_repository)
    monkeypatch.setattr(native,"_repair_input",lambda:request)
    monkeypatch.setattr(native,"RepoSandboxSettings",selected_settings)
    monkeypatch.setattr(native,"_model_transport",transport_factory)
    flow=await native._prepare_native_flow(client,async_db,tmp_path,monkeypatch,low_due=low_due)
    return flow


async def _approve_node_flow(client,flow,*,typescript=False):
    job_id=flow["job_id"];packet=flow["packet"]
    consent=await client.post(f"/api/workflows/repo-repair/{job_id}/code-egress-consent",json={"expected_job_revision":flow["job"]["revision"],"source_packet_digest":packet.artifact_sha256,"expected_source_manifest_digest":packet.source_manifest_digest,"expected_profile_id":"openrouter","acknowledged_selected_source":True,"idempotency_key":"node-egress-"+job_id},headers={"Origin":"http://localhost:3001"})
    assert consent.status_code==200,consent.text
    await flow["dispatcher"].run_pass()
    pending=await client.get(f"/api/workflows/repo-repair/{job_id}")
    assert pending.status_code==200,pending.text
    ready_deadline=time.monotonic()+10
    while pending.json()["status"] in {"running","queued"} and time.monotonic()<ready_deadline:
        await asyncio.sleep(.05)
        pending=await client.get(f"/api/workflows/repo-repair/{job_id}")
        assert pending.status_code==200,pending.text
    payload=pending.json();assert payload["status"]=="awaiting_approval",payload
    assert payload["executor_profile"]=="local:"+PROFILE
    plan=payload["executor_posture"]["execution_plan"]
    assert [command["script"] for command in plan["commands"]]==(["build","test"] if typescript else ["test"])
    assert payload["approval"]["required_permissions"]==["local_host_execution"]
    assert payload["preparation_ready"] is True and payload["execution_ready"] is False
    assert payload["preflight"]["evidence_basis"]=="recorded_job_preflight"
    from src.execution.repo_sandbox import executor_posture_digest
    assert executor_posture_digest(payload["executor_posture_raw"])==payload["executor_posture_digest"]
    proposal=payload["proposal"]
    approval=await client.post(f"/api/approvals/{proposal['approval_id']}/approve",headers={"Origin":"http://localhost:3001"})
    assert approval.status_code==200,approval.text
    resumed=await client.post(f"/api/workflows/repo-repair/{job_id}/resume",json={"expected_job_revision":payload["revision"],"expected_proposal_revision":proposal["revision"],"proposal_id":proposal["proposal_id"],"approval_id":proposal["approval_id"],"idempotency_key":"node-resume-"+job_id},headers={"Origin":"http://localhost:3001"})
    assert resumed.status_code==200,resumed.text
    return payload


@pytest.mark.asyncio
@pytest.mark.parametrize("async_db",["file"],indirect=True)
@pytest.mark.parametrize("typescript",[False,True])
async def test_authenticated_node_work_vertical(client,async_db,tmp_path,monkeypatch,typescript):
    flow=await _prepare_node_flow(client,async_db,tmp_path,monkeypatch,typescript=typescript)
    payload=await _approve_node_flow(client,flow,typescript=typescript)
    job_id=flow["job_id"];proposal=payload["proposal"];plan=payload["executor_posture"]["execution_plan"]
    run=asyncio.create_task(flow["dispatcher"].run_pass())
    await native._wait_for_task_set(run,timeout=45,description="Node durable dispatcher did not settle")
    final=await client.get(f"/api/workflows/repo-repair/{job_id}")
    assert final.status_code==200,final.text
    final_payload=final.json();assert final_payload["status"]=="succeeded",final_payload
    assert final_payload["memory_status"]=="no_learning"
    assert final_payload["execution"]["readback"]["verified"] is True
    assert native._tree_receipt(flow["repository"])==flow["source_before"]
    artifacts=final_payload["execution"]["artifacts"]
    readback=next(item for item in artifacts if item["artifact_type"]=="repo_change_readback_json")
    data=(flow["workspace"]/readback["file_path"]).read_bytes()
    assert hashlib.sha256(data).hexdigest()==readback["content_sha256"]
    manifest=json.loads(data);assert manifest["cleanup_proven"] is True
    assert all(command["argv"][0]==NODE for command in manifest["commands"])
    assert len(flow["transport_calls"])==1
    assert (await durable_job_repository.get_job(job_id))["status"]=="succeeded"
    accounting=await durable_job_repository.inference_accounting_snapshot()
    assert accounting["committed_microusd"]==1,accounting
    assert len(accounting["operations"])==1
    assert accounting["operations"][0]["state"]=="settled"
    from src.workspace.production import ProductionWorkspace,read_lifecycle_receipt
    witness=read_lifecycle_receipt(ProductionWorkspace(host_root=flow["workspace"]))
    assert witness["deployment_binding"]["root_path_digest"]==ProductionWorkspace(host_root=flow["workspace"]).identity_digest
    _retained("seraph-912-"+("ts" if typescript else "js")+"-actual-accounting.json").write_text(json.dumps({"accounting":accounting,"lifecycle_witness":witness,"fixture_transport_only":True},indent=2))
    receipt={"profile":PROFILE,"typescript":typescript,"job_id":job_id,"proposal_id":proposal["proposal_id"],"approval_id":proposal["approval_id"],"status":"succeeded","transport_intercepted":True,"plan":plan,"commands":manifest["commands"],"readback_sha256":readback["content_sha256"],"readback_verified":True,"cleanup_proven":True,"memory_status":"no_learning","original_sha256":flow["source_before"]["sha256"]}
    _retained("seraph-912-"+("ts" if typescript else "js")+"-vertical-receipt.json").write_text(json.dumps(receipt,indent=2))
    _retained("seraph-912-"+("ts" if typescript else "js")+"-pending-ui.json").write_text(json.dumps(payload,indent=2))
    _retained("seraph-912-"+("ts" if typescript else "js")+"-final-api.json").write_text(json.dumps(final_payload,indent=2))
    _retained("seraph-912-"+("ts" if typescript else "js")+"-actual-readback.json").write_bytes(data)



@pytest.mark.asyncio
@pytest.mark.parametrize("async_db",["file"],indirect=True)
@pytest.mark.parametrize("failure",["cancel","supervisor_death","cleanup_deadline","nested_timeout"])
async def test_durable_node_lifecycle_and_retained_slot(client,async_db,tmp_path,monkeypatch,failure):
    running=tmp_path/"owned-node-running"
    script="require('node:fs').writeFileSync("+json.dumps(str(running))+",String(process.pid));setTimeout(()=>{},2000);"
    sentinel=tmp_path/"delayed-unapproved-write"
    if failure=="nested_timeout":
        code="import os,time;from pathlib import Path;os.setsid();[os.close(fd) for fd in (0,1,2)];Path("+repr(str(running))+").write_text(str(os.getpid()));time.sleep(20);Path("+repr(str(sentinel))+").write_text('BAD')"
        script="const p=require('node:child_process').spawn('/usr/bin/python3',['-c',"+json.dumps(code)+"],{stdio:'ignore'});p.unref();setTimeout(()=>{},30000);"
    flow=await _prepare_node_flow(client,async_db,tmp_path,monkeypatch,script=script,wall_seconds=10 if failure in {"cleanup_deadline","nested_timeout"} else 30,low_due=failure=="supervisor_death")
    await _approve_node_flow(client,flow)
    job_id=flow["job_id"]
    fresh=NodeRepoRepairExecutor(workspace_dir=flow["workspace"])
    dispatch=asyncio.create_task(flow["dispatcher"].run_pass())
    owned_pid=None;owned_start=None;stopped=False
    try:
        deadline=time.monotonic()+15
        while not running.exists() and time.monotonic()<deadline:await asyncio.sleep(.01)
        assert running.exists(),"Actual admitted Node command did not run"
        child_pid=int(running.read_text())
        marker=fresh._read_job_marker(job_id)
        assert marker and marker["phase"]=="worker_started"
        owned_pid=marker["pid"];owned_start=marker["pid_start_identity"]
        assert supervisor.start_identity(child_pid)
        assert try_acquire_repo_repair_capacity(flow["workspace"],job_id="unrelated-job") is None
        if failure=="cancel":
            task=await client.get(f"/api/work-board/tasks/{flow['high_task_id']}")
            assert task.status_code==200,task.text
            cancelled=await client.post(f"/api/work-board/tasks/{flow['high_task_id']}/actions",json={"action":"cancel","expected_revision":task.json()["task"]["task_revision"]},headers={"Origin":"http://localhost:3001"})
            assert cancelled.status_code==200,cancelled.text
        elif failure!="nested_timeout":
            stopped=failure=="cleanup_deadline"
            assert supervisor.exact_signal(owned_pid,owned_start,signal.SIGSTOP if stopped else signal.SIGKILL)
        await native._wait_for_task_set(dispatch,timeout=15,description="Actual Node lifecycle dispatch did not settle")
        job=await durable_job_repository.get_job(job_id)
        assert job["status"] in {"cancelled","unknown_external_effect","failed"},job
        if failure in {"supervisor_death","cleanup_deadline"}:
            assert job["status"]=="unknown_external_effect"
        if failure=="nested_timeout":assert job["status"]=="failed",job
        authority={"job_id":job_id,"authority_digest":marker["authority_digest"],"attempt_id":marker["attempt_id"],"fencing_token":marker["fencing_token"]}
        if stopped:
            assert supervisor.exact_signal(owned_pid,owned_start,signal.SIGCONT)
            stopped=False
            await asyncio.to_thread(os.waitpid,owned_pid,0)
        await asyncio.sleep(2.2)
        assert not native._pid_is_running(child_pid)
        assert not sentinel.exists()
        reconciled=fresh.reconcile(authority)
        identity_faults=[]
        second_job=None
        own_start_before=supervisor.start_identity(os.getpid())
        if job["status"]=="unknown_external_effect":
            reservation=next(item for item in job["checkpoints"] if item.get("checkpoint_id")=="repo-repair-execution-reservation")
            assert reservation["payload"]["status"]=="held"
            assert try_acquire_repo_repair_capacity(flow["workspace"],job_id="successor-job") is None
            if failure!="cancel":assert reconciled["status"]=="unknown_external_effect" and reconciled["cleanup_proven"] is False
            if failure=="supervisor_death":
                current=fresh._read_job_marker(job_id)
                for name,pid,start in (("missing",99999999,"not-owned"),("reused",os.getpid(),"not-current-start")):
                    injected={**current,"pid":pid,"pid_start_identity":start}
                    marker_path=fresh._job_marker_directory/fresh._job_marker_name(job_id)
                    marker_path.write_text(json.dumps(injected))
                    assert fresh._read_job_marker(job_id)["pid"]==pid
                    assert fresh._read_job_marker(job_id)["pid_start_identity"]==start
                    assert fresh.cancel(authority=authority)["status"]=="unknown_external_effect"
                    assert fresh.reconcile(authority)["cleanup_proven"] is False
                    assert supervisor.start_identity(os.getpid())==own_start_before
                    assert try_acquire_repo_repair_capacity(flow["workspace"],job_id="restart-successor") is None
                    identity_faults.append({"fault":name,"pid":pid,"start_identity":start,"non_owned_process_start_before":own_start_before,"non_owned_process_start_after":supervisor.start_identity(os.getpid())})
                marker_path.write_text(json.dumps(current))
                async with async_db() as db:
                    low_attempt=(await db.execute(native.select(native.WorkBoardAttempt).where(native.WorkBoardAttempt.task_id==flow["low_task_id"]))).scalars().first()
                    low_packet=(await db.execute(native.select(native.RepoRepairSourcePacketRow).where(native.RepoRepairSourcePacketRow.work_board_task_id==flow["low_task_id"]))).scalars().first()
                assert low_attempt and low_packet
                low_id=str(low_attempt.workflow_run_id)
                low_job=await durable_job_repository.get_job(low_id)
                assert low_job["status"]=="paused"
                await _approve_node_flow(client,{**flow,"job_id":low_id,"job":low_job,"packet":low_packet})
                await flow["dispatcher"].run_pass()
                second_job=await durable_job_repository.get_job(low_id)
                assert second_job["status"]=="blocked",second_job
                assert second_job["failure_reason"]=="repo_repair_execution_busy"
                assert fresh._read_job_marker(low_id) is None
                assert int(running.read_text())==child_pid
                assert try_acquire_repo_repair_capacity(flow["workspace"],job_id="third-job") is None
        else:
            assert reconciled["cleanup_proven"] is True
            successor=try_acquire_repo_repair_capacity(flow["workspace"],job_id="verified-successor")
            assert successor is not None
            successor.release()
        fresh_dispatcher=native.WorkBoardDispatcher(repository=flow["repository_api"],session_provider=async_db,runner_id="node-recovery")
        await fresh_dispatcher.reconcile_linked_attempts()
        recovered_job=await durable_job_repository.get_job(job_id)
        assert len(flow["transport_calls"])==(2 if second_job else 1)
        assert native._tree_receipt(flow["repository"])==flow["source_before"]
        assert supervisor.start_identity(os.getpid())
        retained_marker=fresh._read_job_marker(job_id)
        _retained("seraph-912-"+failure+"-actual-marker.json").write_text(json.dumps(retained_marker,indent=2))
        if retained_marker and isinstance(retained_marker.get("stage_directory"),str):
            actual_result=flow["workspace"]/retained_marker["stage_directory"]/"out"/"supervisor-result.json"
            if actual_result.is_file():
                _retained("seraph-912-"+failure+"-actual-supervisor-result.json").write_bytes(actual_result.read_bytes())
        _retained("seraph-912-"+failure+"-durable-receipt.json").write_text(json.dumps({"job_id":job_id,"original_marker_attempt_id":marker["attempt_id"],"original_marker_fencing_token":marker["fencing_token"],"final_first_job_fencing_token":(job.get("lease") or {}).get("fencing_token"),"final_first_job_lease":job.get("lease"),"recovered_first_job_lease":recovered_job.get("lease"),"recovered_first_job_status":recovered_job["status"],"second_accepted_job":second_job,"failure":failure,"status":job["status"],"same_root_recovery":True,"successor_blocked":job["status"]=="unknown_external_effect","owned_child_quiescent":True,"delayed_sentinel_absent":True,"restart_identity_faults_rejected":identity_faults,"reconciled":reconciled,"model_calls":len(flow["transport_calls"])},indent=2))
    finally:
        if stopped and owned_pid and owned_start:
            supervisor.exact_signal(owned_pid,owned_start,signal.SIGCONT)
            with suppress(ChildProcessError):await asyncio.to_thread(os.waitpid,owned_pid,0)
        await native._quiesce_tasks(dispatch,timeout=15)
        # Test-only descriptor cleanup after proving the unresolved durable
        # row and production quarantine retain ownership. No product settle.
        lane=_QUARANTINED_LANES.get(str(flow["workspace"]))
        if lane:lane.clear_quarantine()


@pytest.mark.asyncio
@pytest.mark.parametrize("async_db",["file"],indirect=True)
async def test_authenticated_node_missing_recorded_preflight_blocks_display(client,async_db,tmp_path,monkeypatch):
    from src.db.models import WorkflowRunState
    from sqlmodel import select
    flow=await _prepare_node_flow(client,async_db,tmp_path,monkeypatch)
    job_id=flow["job_id"]
    pending=await _approve_node_flow(client,flow)
    assert pending["preparation_ready"] is True
    original_digest=pending["authority_digest"]
    async with async_db() as db:
        row=(await db.execute(select(WorkflowRunState).where(WorkflowRunState.run_identity==job_id))).scalar_one()
        original=json.loads(row.checkpoint_receipts_json)
    for fault in ("missing","blocked"):
        checkpoints=json.loads(json.dumps(original))
        if fault=="missing":
            checkpoints=[item for item in checkpoints if item.get("checkpoint_id") not in {"repo-repair-preflight",f"repo-repair-preflight:{job_id}"}]
        else:
            for item in checkpoints:
                if item.get("checkpoint_id") in {"repo-repair-preflight",f"repo-repair-preflight:{job_id}"}:
                    item["payload"]["receipt"]={"ok":False,"status":"blocked"}
        async with async_db() as db:
            row=(await db.execute(select(WorkflowRunState).where(WorkflowRunState.run_identity==job_id))).scalar_one()
            row.checkpoint_receipts_json=json.dumps(checkpoints)
            await db.commit()
        response=await client.get(f"/api/workflows/repo-repair/{job_id}")
        assert response.status_code==200,response.text
        payload=response.json()
        assert payload["preparation_ready"] is False
        assert payload["execution_ready"] is False
        assert payload["preflight"]["status"]=="blocked"
        assert payload["authority_digest"]==original_digest


@pytest.mark.asyncio
@pytest.mark.parametrize("async_db",["file"],indirect=True)
async def test_integrated_original_python_native_vertical(client,async_db,tmp_path,monkeypatch):
    _prepare_accounting_fixture(tmp_path,monkeypatch)
    original_factory=native._model_transport
    def factory(calls):
        original_transport=original_factory(calls)
        def transport(**kwargs):
            response,raw=original_transport(**kwargs)
            return response,{**raw,"id":"gen-python-fixture","usage":{**raw.get("usage",{}),"cost":"0.000001"}}
        return transport
    monkeypatch.setattr(native,"_model_transport",factory)
    await native.test_local_native_repo_repair_api_vertical(client,async_db,tmp_path,monkeypatch)


@pytest.mark.asyncio
@pytest.mark.parametrize("async_db",["file"],indirect=True)
async def test_cancelled_node_physical_cleanup_recovery_and_fresh_job(client,async_db,tmp_path,monkeypatch):
    from dataclasses import replace
    from datetime import datetime,timedelta,timezone
    from src.api.workflows import _repo_change_recovery_authority,_repo_change_dispatch_contract
    from src.db.models import ApprovalRequest,Goal,WorkflowRunState
    from src.workflows.job_runtime import NodeProcessCleanupSettlement,DurableJobError

    running=tmp_path/"actual-running"
    script="require('node:fs').writeFileSync("+json.dumps(str(running))+",String(process.pid));setTimeout(()=>{},3000);"
    flow=await _prepare_node_flow(client,async_db,tmp_path,monkeypatch,script=script,wall_seconds=30,low_due=True)
    pending=await _approve_node_flow(client,flow)
    job_id=flow["job_id"]
    fresh=NodeRepoRepairExecutor(workspace_dir=flow["workspace"])
    dispatch=asyncio.create_task(flow["dispatcher"].run_pass())
    try:
        deadline=time.monotonic()+15
        while not running.exists() and time.monotonic()<deadline:await asyncio.sleep(.01)
        assert running.exists()
        task=await client.get(f"/api/work-board/tasks/{flow['high_task_id']}")
        cancelled=await client.post(f"/api/work-board/tasks/{flow['high_task_id']}/actions",json={"action":"cancel","expected_revision":task.json()["task"]["task_revision"]},headers={"Origin":"http://localhost:3001"})
        assert cancelled.status_code==200,cancelled.text
        await native._wait_for_task_set(dispatch,timeout=15,description="Cancelled Node durable dispatcher did not settle")
        job=await durable_job_repository.get_job(job_id)
        assert job["status"]=="unknown_external_effect",job
        deadline=time.monotonic()+10
        while time.monotonic()<deadline:
            marker=fresh._read_job_marker(job_id)
            if marker and marker.get("cleanup_proven") is True:break
            await asyncio.sleep(.01)
        assert marker["status"]=="cancelled" and marker["cleanup_proven"] is True,marker
        assert marker["process_cleanup"]["process_cleanup"]["oracle"]=="linux_subreaper_waitpid_echild"
        assert try_acquire_repo_repair_capacity(flow["workspace"],job_id="unrelated-before-recovery") is None
        authority=await _repo_change_recovery_authority(job)
        okay,reason,original_dispatch=_repo_change_dispatch_contract(job,authority)
        assert okay,reason
        request=NodeProcessCleanupSettlement(job_id,int(job["revision"]),flow["owner"].principal_id,flow["owner"].session_id,authority,original_dispatch)
        for bad_request in (replace(request,expected_revision=request.expected_revision-1),replace(request,owner_session_id="different-original-root")):
            with pytest.raises(DurableJobError):await durable_job_repository.settle_node_process_cleanup(bad_request)
        negatives={"job_id":"other-job","attempt_id":"other-attempt","authority_digest":"f"*64,
                   "fencing_token":marker["fencing_token"]+1,"supervisor_token":"wrong-token","pid":os.getpid(),
                   "pid_start_identity":"reused-start","supervisor_source_sha256":"e"*64,"profile":"repo-python-pytest-v1",
                   "stage_binding":{},"base_digest":"d"*64,"posture_digest":"c"*64,
                   "process_cleanup":{**marker["process_cleanup"],"process_cleanup":{"cleanup_proven":True,"oracle":"not_echild"}}}
        marker_path=fresh._job_marker_directory/fresh._job_marker_name(job_id)
        accounting_before=await durable_job_repository.inference_accounting_snapshot()
        task_before_recovery=(await client.get(f"/api/work-board/tasks/{flow['high_task_id']}" )).json()["task"]
        held_projection=await client.get(f"/api/workflows/repo-repair/{job_id}")
        assert held_projection.status_code==200,held_projection.text
        assert held_projection.json()["execution"]["process_cleanup"]=={
            "status":"held", "physical_capacity_released":False,
            "cleanup_receipt_verified":False, "readback_scope":None,
        }
        _retained("node-repair-cleanup-held-api.json").write_text(json.dumps(held_projection.json(),indent=2))
        for field,value in negatives.items():
            marker_path.write_text(json.dumps({**marker,field:value}))
            blocked=await client.post(f"/api/workflows/repo-change/{job_id}/recover",headers={"Origin":"http://localhost:3001"})
            assert blocked.status_code==200,blocked.text
            assert blocked.json()["status"]=="blocked",(field,blocked.json())
            assert (await durable_job_repository.get_job(job_id))["revision"]==job["revision"]
            assert try_acquire_repo_repair_capacity(flow["workspace"],job_id="negative-successor") is None
        marker_path.write_text(json.dumps(marker))
        async with async_db() as db:
            proposal_row=(await db.execute(native.select(native.RepoRepairProposalRow).where(native.RepoRepairProposalRow.workflow_run_id==job_id))).scalar_one()
            proposal_digest=proposal_row.authority_digest
            proposal_row.authority_digest="f"*64
            await db.commit()
        digest_blocked=await client.post(f"/api/workflows/repo-change/{job_id}/recover",headers={"Origin":"http://localhost:3001"})
        assert digest_blocked.status_code==200 and digest_blocked.json()["status"]=="blocked",digest_blocked.text
        assert (await durable_job_repository.get_job(job_id))["revision"]==job["revision"]
        async with async_db() as db:
            proposal_row=(await db.execute(native.select(native.RepoRepairProposalRow).where(native.RepoRepairProposalRow.workflow_run_id==job_id))).scalar_one()
            proposal_row.authority_digest=proposal_digest
            goal=await db.get(Goal,job["goal_id"]);goal.revision+=1
            approval=await db.get(ApprovalRequest,pending["proposal"]["approval_id"])
            approval.expires_at=datetime.now(timezone.utc)-timedelta(seconds=1)
            await db.commit()
        original_row=None
        async with async_db() as db:
            row=(await db.execute(native.select(WorkflowRunState).where(WorkflowRunState.run_identity==job_id))).scalar_one()
            original_row={field:getattr(row,field) for field in ("status","fencing_token","lease_owner","lease_expires_at","result_digest","result_summary","effect_receipts_json","artifact_receipts_json","declared_authority_json","goal_revision","budget_digest","failure_reason")}
        recovered=await client.post(f"/api/workflows/repo-change/{job_id}/recover",headers={"Origin":"http://localhost:3001"})
        assert recovered.status_code==200,recovered.text
        result=recovered.json()
        assert result.get("physical_capacity_released") is True,result
        assert result["status"]=="unknown_external_effect" and result["readback_scope"]=="process_cleanup_only"
        assert result["cleanup_receipt_verified"] is True
        settled=await durable_job_repository.get_job(job_id)
        release=next(item["payload"] for item in settled["checkpoints"] if item["checkpoint_id"]=="repo-repair-execution-release")
        assert release["process_cleanup_readback_sha256"] and "readback_verified" not in release
        repeated=await client.post(f"/api/workflows/repo-change/{job_id}/recover",headers={"Origin":"http://localhost:3001"})
        assert repeated.status_code==200 and repeated.json().get("physical_capacity_released") is True,repeated.text
        assert (await durable_job_repository.get_job(job_id))["revision"]==settled["revision"]
        # A fresh repository instance reads only the durable row; no POST
        # result or in-memory cleanup state is used by the Inspector GET.
        from src.api import workflows as workflows_api
        from src.workflows.job_runtime import DurableJobRepository
        restarted_repository=DurableJobRepository()
        monkeypatch.setattr(workflows_api,"durable_job_repository",restarted_repository)
        durable_projection=await client.get(f"/api/workflows/repo-repair/{job_id}")
        assert durable_projection.status_code==200,durable_projection.text
        inspector=durable_projection.json()
        cleanup=inspector["execution"]["process_cleanup"]
        assert cleanup=={"status":"released", "physical_capacity_released":True,"cleanup_receipt_verified":True,
                         "readback_scope":"process_cleanup_only", "job_id":job_id,"attempt_id":original_dispatch["attempt_id"],
                         "fencing_token":original_dispatch["fencing_token"],"authority_digest":job["authority_digest"],
                         "process_cleanup_readback_sha256":release["process_cleanup_readback_sha256"]}
        assert inspector["status"]=="unknown_external_effect" and inspector["memory_status"]=="no_learning"
        assert inspector["execution"]["readback"] is None
        assert not {"supervisor_token","stage_binding","pid","argv","private_path"}.intersection(cleanup)
        _retained("node-repair-cleanup-released-api.json").write_text(json.dumps(inspector,indent=2))
        async with async_db() as db:
            row=(await db.execute(native.select(WorkflowRunState).where(WorkflowRunState.run_identity==job_id))).scalar_one()
            original_checkpoints=row.checkpoint_receipts_json
        for bad_field,bad_value in {"job_id":"wrong-job", "attempt_id":"wrong-attempt", "fence":original_dispatch["fencing_token"]+1,
                                   "authority_digest":"a"*64,"cleanup_receipt_verified":False,"process_cleanup_readback_sha256":"invalid"}.items():
            history=json.loads(original_checkpoints)
            next(item for item in history if item["checkpoint_id"]=="repo-repair-execution-release")["payload"][bad_field]=bad_value
            async with async_db() as db:
                row=(await db.execute(native.select(WorkflowRunState).where(WorkflowRunState.run_identity==job_id))).scalar_one()
                row.checkpoint_receipts_json=json.dumps(history)
                await db.commit()
            invalid_projection=await client.get(f"/api/workflows/repo-repair/{job_id}")
            assert invalid_projection.status_code==200,invalid_projection.text
            assert invalid_projection.json()["execution"]["process_cleanup"]["physical_capacity_released"] is False,bad_field
        async with async_db() as db:
            row=(await db.execute(native.select(WorkflowRunState).where(WorkflowRunState.run_identity==job_id))).scalar_one()
            row.checkpoint_receipts_json=original_checkpoints
            await db.commit()
        async with async_db() as db:
            row=(await db.execute(native.select(WorkflowRunState).where(WorkflowRunState.run_identity==job_id))).scalar_one()
            assert {field:getattr(row,field) for field in original_row}==original_row
        assert await durable_job_repository.inference_accounting_snapshot()==accounting_before
        task_after=await client.get(f"/api/work-board/tasks/{flow['high_task_id']}")
        assert task_after.json()["task"]["status"]==task_before_recovery["status"]
        assert task_after.json()["task"]["task_revision"]==task_before_recovery["task_revision"]
        successor=try_acquire_repo_repair_capacity(flow["workspace"],job_id="physical-successor")
        assert successor is not None;successor.release()
        async with async_db() as db:
            low_attempt=(await db.execute(native.select(native.WorkBoardAttempt).where(native.WorkBoardAttempt.task_id==flow["low_task_id"]))).scalars().first()
            low_packet=(await db.execute(native.select(native.RepoRepairSourcePacketRow).where(native.RepoRepairSourcePacketRow.work_board_task_id==flow["low_task_id"]))).scalars().first()
        low_id=str(low_attempt.workflow_run_id);low_job=await durable_job_repository.get_job(low_id)
        await _approve_node_flow(client,{**flow,"job_id":low_id,"job":low_job,"packet":low_packet})
        await flow["dispatcher"].run_pass()
        second=await durable_job_repository.get_job(low_id)
        assert second["status"]=="succeeded",second
        assert fresh._read_job_marker(low_id)["cleanup_proven"] is True
        assert len(flow["transport_calls"])==2
        _retained("physical-cleanup-recovery.json").write_text(json.dumps({"first_job":job_id,"first_status":settled["status"],"negative_marker_bindings":list(negatives),"stale_cas_rejected":True,"wrong_root_rejected":True,"changed_goal_and_expired_approval":True,"original_row_unchanged":True,"accounting_unchanged":True,"release":release,"second_job":low_id,"second_status":second["status"],"actual_echild_marker":marker},indent=2))
    finally:
        await native._quiesce_tasks(dispatch,timeout=15)
        lane=_QUARANTINED_LANES.get(str(flow["workspace"]))
        if lane:lane.clear_quarantine()


@pytest.mark.asyncio
@pytest.mark.parametrize("async_db",["file"],indirect=True)
async def test_default_python_authenticated_journey_without_node_dependency(client,async_db,tmp_path,monkeypatch):
    _prepare_accounting_fixture(tmp_path,monkeypatch)
    original_transport=native._model_transport
    def costed_transport(calls):
        original=original_transport(calls)
        def transport(**kwargs):
            response,metadata=original(**kwargs)
            return response,{**metadata,"id":"gen-python-fixture","usage":{"prompt_tokens":1,"completion_tokens":1,"total_tokens":2,"cost":"0.000001"}}
        return transport
    monkeypatch.setattr(native,"_model_transport",costed_transport)
    await native.test_local_native_repo_repair_api_vertical(client,async_db,tmp_path,monkeypatch)
