from __future__ import annotations
import difflib
import errno
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import threading
import time
from dataclasses import replace
from types import SimpleNamespace

import pytest
from config.settings import RepoSandboxSettings
from src.execution.repo_node import NodeRepoRepairExecutor, PROFILE, execution_plan, normalize_selection
from src.execution.repo_sandbox import RepoSandboxError, RepoSandboxJob
from src.execution import repo_supervisor as supervisor

NODE = os.environ.get("SERAPH_TEST_NODE_RUNTIME") or shutil.which("node") or ""
TYPESCRIPT = os.environ.get("SERAPH_TEST_TYPESCRIPT_ROOT") or str(Path(__file__).resolve().parents[2]/"frontend/node_modules/typescript")


def require_native_platform():
    try:
        supervisor.platform_ready()
    except (ValueError,OSError) as exc:
        pytest.skip("Optional native Node supervisor unavailable: "+str(exc))


def require_native_runtime():
    require_native_platform()
    if not NODE:
        pytest.skip("Optional Node24 runtime missing; set SERAPH_TEST_NODE_RUNTIME")
    version=subprocess.run([NODE,"--version"],capture_output=True,text=True,timeout=2).stdout.strip()
    if not version.startswith("v24."):
        pytest.skip("Optional selected profile requires Node24; found "+version)


def make_fixture(tmp_path: Path, *, typescript=False, script=None, native=True):
    if native: require_native_runtime()
    if typescript and not (Path(TYPESCRIPT)/"package.json").is_file():
        pytest.skip("Optional TypeScript dependency missing; set SERAPH_TEST_TYPESCRIPT_ROOT")
    workspace=tmp_path/"workspace";workspace.mkdir(mode=0o700)
    repo=workspace/"repo";(repo/"src").mkdir(parents=True);(repo/"tests").mkdir()
    if typescript:
        (repo/"src/app.ts").write_text("export const VALUE: number = 1;\n")
        (repo/"tests/app.test.js").write_text("const {VALUE}=require('../dist/app.js'); require('node:assert/strict').equal(VALUE,2);\n")
        (repo/"tsconfig.json").write_text(json.dumps({"compilerOptions":{"target":"ES2020","module":"commonjs","outDir":"dist","rootDir":"src"},"include":["src/*.ts"]}))
        shutil.copytree(TYPESCRIPT,repo/"node_modules/typescript")
        version=json.loads((repo/"node_modules/typescript/package.json").read_text())["version"]
        packages={"node_modules/typescript":{"version":version}}
        scripts={"build":"tsc --project tsconfig.json","test":"node --test tests/app.test.js"}
        path="src/app.ts";old="export const VALUE: number = 1;\n";new="export const VALUE: number = 2;\n"
        args=("npm","run","build","test")
    else:
        old="exports.VALUE = 1;\n";new="exports.VALUE = 2;\n";path="src/app.js"
        (repo/path).write_text(old)
        (repo/"tests/app.test.js").write_text(script or "const {VALUE}=require('../src/app.js'); require('node:assert/strict').equal(VALUE,2); console.log('NODE_TEST_OK');\n")
        scripts={"test":"node --test tests/app.test.js"};packages={};args=("npm","test")
    scripts.update(pretest="node hook.js",posttest="node hook.js",prebuild="node hook.js",postbuild="node hook.js")
    (repo/"hook.js").write_text("require('node:fs').writeFileSync('hook-ran','BAD');\n")
    (repo/"package.json").write_text(json.dumps({"name":"bounded-fixture","version":"1.0.0","scripts":scripts}))
    (repo/"package-lock.json").write_text(json.dumps({"name":"bounded-fixture","lockfileVersion":3,"packages":packages}))
    config=RepoSandboxSettings(profile=PROFILE,node_runtime_path=NODE,enabled=True)
    executor=NodeRepoRepairExecutor(config,workspace_dir=workspace)
    allowed=(path,"tests/app.test.js")
    snapshot=executor.snapshot_repository(repo,workspace/"preview")
    patch="".join(difflib.unified_diff(old.splitlines(True),new.splitlines(True),fromfile="a/"+path,tofile="b/"+path)).encode()
    preflight=executor.preflight({"repository_ref":str(repo),"test_args":args,"allowed_paths":allowed})
    if native: assert preflight.ok,preflight.reason
    job=RepoSandboxJob("node-job",str(repo),patch,allowed,args,"authority-node",snapshot.digest,deadline_seconds=30,expected_posture_digest=preflight.posture_digest)
    return executor,repo,job


@pytest.mark.parametrize("typescript",[False,True])
def test_real_node_and_typescript_staged_execution(tmp_path,typescript):
    executor,repo,job=make_fixture(tmp_path,typescript=typescript)
    source=(repo/job.allowed_paths[0]).read_bytes()
    result=executor.execute_job(job)
    assert result["status"]=="succeeded",result
    assert result["manifest"]["cleanup_proven"] is True
    assert result["manifest"]["process_cleanup"]["oracle"]=="linux_subreaper_waitpid_echild"
    assert result["manifest"]["source_original_unchanged"] is True
    assert (repo/job.allowed_paths[0]).read_bytes()==source
    assert result["outputs"]["diff.patch"]
    assert not (repo/"hook-ran").exists()
    assert [command["script"] for command in result["manifest"]["commands"]]==(["build","test"] if typescript else ["test"])
    assert all(command["argv"][0]==NODE for command in result["manifest"]["commands"])
    assert result["learning"]=="no_learning"


@pytest.mark.parametrize("body",["node --test tests/*.test.js","npm test","npx vitest","node tests/app.test.js && echo bad","NODE_OPTIONS=x node tests/app.test.js","node --watch tests/app.test.js","tsc -b","node ./tests/app.test.js","node --test ../escape.test.js","node tests/app.test.js "])
def test_unsupported_script_forms_block(tmp_path,body):
    from src.execution.repo_node import canonical_node_command
    with pytest.raises(RepoSandboxError): canonical_node_command("test",body,NODE or "/missing/node")


@pytest.mark.parametrize("path",["package.json","package-lock.json","src/app.js","tests/app.test.js"])
def test_execution_input_or_source_drift_invalidates_approval(tmp_path,path):
    executor,repo,job=make_fixture(tmp_path)
    (repo/path).write_text((repo/path).read_text()+"\n")
    with pytest.raises(RepoSandboxError):executor.execute_job(job)


def test_dependencies_never_follow_foreign_links(tmp_path):
    executor,repo,job=make_fixture(tmp_path,native=False)
    (repo/"node_modules").mkdir();outside=tmp_path/"foreign";outside.write_text("secret")
    (repo/"node_modules/foreign").symlink_to(outside)
    with pytest.raises(RepoSandboxError):executor.snapshot_repository(repo,executor.workspace_dir/"foreign-preview")


def test_running_command_cancel_uses_owned_supervisor(tmp_path):
    marker=tmp_path/"running"
    body="require('node:fs').writeFileSync("+json.dumps(str(marker))+",String(process.pid)); setTimeout(()=>{},10000);\n"
    executor,repo,job=make_fixture(tmp_path,script=body)
    result=[];errors=[]
    def run():
        try:result.append(executor.execute_job(job))
        except BaseException as exc:errors.append(exc)
    thread=threading.Thread(target=run);thread.start()
    deadline=time.monotonic()+10
    while not marker.exists() and time.monotonic()<deadline:time.sleep(.01)
    assert marker.exists()
    child=int(marker.read_text());assert supervisor.start_identity(child)
    fresh=NodeRepoRepairExecutor(executor.config,workspace_dir=executor.workspace_dir)
    cancel=fresh.cancel(job_id=job.job_id,authority={"authority_digest":job.authority_digest})
    assert cancel["status"]=="cancel_requested"
    thread.join(10)
    assert not thread.is_alive() and not errors,errors
    assert result[0]["status"]=="cancelled"
    assert result[0]["cleanup"]["cleanup_proven"]
    assert supervisor.start_identity(child) is None


def test_platform_and_pid_reuse_fail_closed(monkeypatch):
    monkeypatch.setattr(supervisor.sys,"platform","darwin")
    with pytest.raises(ValueError):supervisor.platform_ready()
    assert supervisor.exact_signal(os.getpid(),"wrong-start-token",signal.SIGTERM) is False


def test_cleanup_expired_budget_never_claims_proof():
    assert supervisor.cleanup(time.monotonic()-1)["cleanup_proven"] is False


@pytest.mark.parametrize("keep_parent",[False,True])
def test_detached_closed_pipe_descendant_rejected_and_reaped(tmp_path,keep_parent):
    marker=tmp_path/"detached-pid";sentinel=tmp_path/"late-write"
    code="import os,time;from pathlib import Path;os.setsid();[os.close(fd) for fd in (0,1,2)];Path("+repr(str(marker))+").write_text(str(os.getpid()));time.sleep(4);Path("+repr(str(sentinel))+").write_text('BAD')"
    script="const {spawn}=require('node:child_process');const p=spawn('/usr/bin/python3',['-c',"+json.dumps(code)+"],{stdio:'ignore'});p.unref();"+("setTimeout(()=>{},10000);" if keep_parent else "")
    executor,repo,job=make_fixture(tmp_path,script=script)
    job=replace(job,deadline_seconds=3)
    try:
        result=executor.execute_job(job)
    except RepoSandboxError as exc:
        # The strict total wall bound includes readback. Slow test hosts may
        # reap the descendant inside that bound yet lack time to adopt output.
        assert exc.terminal_status=="unknown_external_effect"
        assert "deadline" in str(exc) or "timed out" in str(exc) or "terminal cleanup is unproven" in str(exc)
        durable=executor._read_job_marker(job.job_id)
        assert durable["status"]=="unknown_external_effect" and durable["cleanup_proven"] is False
        # An expired proof is never adopted. The authenticated ten-second
        # nested-timeout vertical independently requires terminal ECHILD.
        terminal_path=executor.workspace_dir/durable["stage_directory"]/"out"/"supervisor-result.json"
        if terminal_path.exists():
            result=json.loads(executor._read_private_output(terminal_path.parent,"supervisor-result.json"))
            if result["cleanup_proven"] is True:
                assert result["process_cleanup"]["oracle"]=="linux_subreaper_waitpid_echild"
    else:
        assert result["status"]=="failed"
        assert result["cleanup"]["cleanup_proven"]
    if marker.exists():assert supervisor.start_identity(int(marker.read_text())) is None
    time.sleep(1.1)
    assert not sentinel.exists()


def test_abnormal_supervisor_death_preserves_durable_unknown(tmp_path):
    running=tmp_path/"running-node"
    body="require('node:fs').writeFileSync("+json.dumps(str(running))+",String(process.pid));setTimeout(()=>{},2000);"
    executor,repo,job=make_fixture(tmp_path,script=body)
    errors=[]
    def run():
        try:executor.execute_job(job)
        except RepoSandboxError as exc:errors.append(exc)
    thread=threading.Thread(target=run);thread.start()
    deadline=time.monotonic()+10
    while not running.exists() and time.monotonic()<deadline:time.sleep(.01)
    assert running.exists()
    marker=executor._read_job_marker(job.job_id)
    assert supervisor.exact_signal(marker["pid"],marker["pid_start_identity"],signal.SIGKILL)
    thread.join(5)
    assert not thread.is_alive() and errors
    fresh=NodeRepoRepairExecutor(executor.config,workspace_dir=executor.workspace_dir)
    receipt=fresh.reconcile({"job_id":job.job_id,"authority_digest":job.authority_digest})
    assert receipt["status"]=="unknown_external_effect" and not receipt["cleanup_proven"]
    assert executor._read_job_marker(job.job_id)["cleanup_proven"] is False
    with pytest.raises(RepoSandboxError):fresh.execute_job(job)
    # This deliberately owned fixture self-exits in 2 seconds. No unrelated
    # host PID is killed to manufacture a restart cleanup receipt.
    time.sleep(2.2)


def test_restart_missing_or_reused_identity_never_signals_unrelated(tmp_path):
    require_native_platform()  # This receipt inspects an actual Linux PID/start identity.
    executor,repo,job=make_fixture(tmp_path,native=False)
    for pid,start in ((99999999,"missing"),(os.getpid(),"reused-start")):
        executor._write_job_marker(job.job_id,{"job_id":job.job_id,"profile":PROFILE,"authority_digest":job.authority_digest,"attempt_id":"legacy-attempt","pid":pid,"pid_start_identity":start,"cleanup_proven":False,"phase":"worker_started"})
        fresh=NodeRepoRepairExecutor(executor.config,workspace_dir=executor.workspace_dir)
        assert fresh.cancel(job_id=job.job_id,authority={"authority_digest":job.authority_digest})["status"]=="unknown_external_effect"
        assert not fresh.reconcile({"job_id":job.job_id})["cleanup_proven"]
        assert supervisor.start_identity(os.getpid())


def test_actual_pidfd_errno_and_unsupported_architecture(monkeypatch):
    require_native_platform()
    with pytest.raises(OSError):supervisor.pidfd_open(-1)
    with pytest.raises(OSError):supervisor.pidfd_send(-1,0)
    monkeypatch.setattr(supervisor.os,"uname",lambda:SimpleNamespace(machine="armv7l"))
    with pytest.raises(ValueError):supervisor.platform_ready()


def test_selected_node_platform_block_does_not_block_python_core(tmp_path,monkeypatch):
    executor,repo,job=make_fixture(tmp_path,native=False)
    monkeypatch.setattr(supervisor.sys,"platform","darwin")
    assert executor.preflight().ok is False
    from src.execution.repo_sandbox import build_repo_repair_executor,LocalRepoRepairExecutor
    config=RepoSandboxSettings(enabled=True)
    assert isinstance(build_repo_repair_executor(config),LocalRepoRepairExecutor)
    python=LocalRepoRepairExecutor(config,workspace_dir=executor.workspace_dir)
    assert python.preflight().ok is True


def test_node_docker_selection_reports_unverified_posture(tmp_path):
    executor,repo,job=make_fixture(tmp_path,native=False)
    executor.config=executor.config.model_copy(update={"executor_kind":"docker_rootless"})
    preflight=executor.preflight()
    assert not preflight.ok and preflight.reason=="node_docker_profile_unverified"
    assert preflight.posture["isolation_claim"]=="unverified"
    assert "host_access" not in preflight.posture


@pytest.mark.parametrize("code",[errno.ENOSYS,errno.EPERM])
def test_kernel_facility_failures_block_without_pid_signal_fallback(monkeypatch,code):
    def unavailable(pid):raise OSError(code,os.strerror(code))
    monkeypatch.setattr(supervisor.sys,"platform","linux")
    monkeypatch.setattr(supervisor.os,"uname",lambda:SimpleNamespace(machine="x86_64"))
    monkeypatch.setattr(supervisor.Path,"exists",lambda self:True)
    monkeypatch.setattr(supervisor,"pidfd_open",unavailable)
    with pytest.raises(OSError) as error:supervisor.platform_ready()
    assert error.value.errno==code


def test_node_launch_strips_dynamic_loader_and_runtime_environment(tmp_path,monkeypatch):
    names=("LD_PRELOAD","LD_LIBRARY_PATH","DYLD_INSERT_LIBRARIES","PYTHONPATH","NODE_OPTIONS","npm_config_node_options")
    for name in names:monkeypatch.setenv(name,"/tmp/deliberately-nonexistent-owned-input")
    sentinel=tmp_path/"environment-scrubbed"
    script="for(const name of "+json.dumps(names)+")if(process.env[name])throw Error(name);require('node:fs').writeFileSync("+json.dumps(str(sentinel))+",'ENV_SCRUB_OK');"
    executor,repo,job=make_fixture(tmp_path,script=script)
    result=executor.execute_job(job)
    assert result["status"]=="succeeded",result
    assert sentinel.read_text()=="ENV_SCRUB_OK"


@pytest.mark.parametrize("field",["pid","pid_start_identity","supervisor_token","process_cleanup"])
def test_terminal_node_missing_restart_proof_never_adopts_cleanup(tmp_path,field):
    executor,repo,job=make_fixture(tmp_path)
    assert executor.execute_job(job)["status"]=="succeeded"
    marker=executor._read_job_marker(job.job_id)
    del marker[field]
    executor._write_job_marker(job.job_id,marker)
    fresh=NodeRepoRepairExecutor(executor.config,workspace_dir=executor.workspace_dir)
    receipt=fresh.reconcile({"job_id":job.job_id,"authority_digest":job.authority_digest})
    assert receipt["status"]=="unknown_external_effect" and receipt["cleanup_proven"] is False


def test_node_unknown_exception_mapping_preserves_old_python_behavior():
    from src.api.workflows import _repo_change_execution_failure_status
    unknown=RepoSandboxError("owned cleanup unproven",terminal_status="unknown_external_effect")
    assert _repo_change_execution_failure_status(unknown,{"sandbox_profile":PROFILE})=="unknown_external_effect"
    assert _repo_change_execution_failure_status(unknown,{"sandbox_profile":"repo-python-pytest-v1"})=="blocked"
    assert _repo_change_execution_failure_status(unknown,{})=="blocked"
    failed=RepoSandboxError("test failed",terminal_status="failed")
    assert _repo_change_execution_failure_status(failed,{"sandbox_profile":"repo-python-pytest-v1"})=="failed"


def test_private_marker_lock_serializes_competing_process(tmp_path):
    from src.execution.repo_sandbox import LocalRepoRepairExecutor
    executor=LocalRepoRepairExecutor(RepoSandboxSettings(),workspace_dir=tmp_path)
    executor._write_job_marker("locked",{"job_id":"locked","status":"running"})
    code="from pathlib import Path;from config.settings import RepoSandboxSettings;from src.execution.repo_sandbox import LocalRepoRepairExecutor;import sys; e=LocalRepoRepairExecutor(RepoSandboxSettings(),workspace_dir=Path(sys.argv[1]));\nwith e._job_marker_lock('locked'):\n print('LOCKED',flush=True);sys.stdin.readline()\n"
    process=subprocess.Popen([sys.executable,"-c",code,str(tmp_path)],cwd=Path(__file__).resolve().parents[1],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    try:
        assert process.stdout.readline().strip()=="LOCKED"
        with pytest.raises(RepoSandboxError,match="busy"):
            with executor._job_marker_lock("locked",timeout_seconds=.05):pass
    finally:
        process.communicate("release\n",timeout=5)
    with executor._job_marker_lock("locked",timeout_seconds=.05):pass
    metadata=(executor._job_marker_directory/(executor._job_marker_name("locked")+".lock")).stat()
    assert metadata.st_uid==os.getuid() and metadata.st_mode & 0o777==0o600 and metadata.st_nlink==1


def test_cancellation_check_to_rename_competing_process_is_monotonic(tmp_path,monkeypatch):
    executor=NodeRepoRepairExecutor(RepoSandboxSettings(profile=PROFILE),workspace_dir=tmp_path)
    executor._write_job_marker("race",{"job_id":"race","profile":PROFILE,"attempt_id":"legacy-attempt","status":"running"})
    ready=tmp_path/"cancel-ready";done=tmp_path/"cancel-done"
    code="from pathlib import Path;from config.settings import RepoSandboxSettings;from src.execution.repo_node import NodeRepoRepairExecutor,PROFILE;import sys; e=NodeRepoRepairExecutor(RepoSandboxSettings(profile=PROFILE),workspace_dir=Path(sys.argv[1]));Path(sys.argv[2]).write_text('ready');e.cancel(job_id='race');Path(sys.argv[3]).write_text('done')"
    process=None;rename=os.rename
    def interpose(source,target,**kwargs):
        nonlocal process
        if target==executor._job_marker_name("race") and process is None:
            process=subprocess.Popen([sys.executable,"-c",code,str(tmp_path),str(ready),str(done)],cwd=Path(__file__).resolve().parents[1],stdout=subprocess.PIPE,stderr=subprocess.PIPE)
            deadline=time.monotonic()+1
            while not ready.exists() and time.monotonic()<deadline:time.sleep(.005)
            assert ready.exists()
            time.sleep(.05)
            assert not done.exists(),"Competing cancellation committed while writer held the marker lock"
        return rename(source,target,**kwargs)
    monkeypatch.setattr(os,"rename",interpose)
    executor._write_job_marker("race",{"job_id":"race","profile":PROFILE,"attempt_id":"legacy-attempt","status":"running","phase":"worker_started"})
    assert process is not None
    stdout,stderr=process.communicate(timeout=5)
    assert process.returncode==0,stderr
    assert done.exists()
    executor._write_job_marker("race",{"job_id":"race","profile":PROFILE,"attempt_id":"legacy-attempt","status":"running"})
    marker=executor._read_job_marker("race")
    assert marker["status"]=="cancellation_requested" and marker["cancellation_requested"] is True


def test_committed_predispatch_cancellation_executes_no_node_command(tmp_path):
    sentinel=tmp_path/"command-started"
    executor,repo,job=make_fixture(tmp_path,script="require('node:fs').writeFileSync("+json.dumps(str(sentinel))+",'BAD');")
    def commit_cancel():
        fresh=NodeRepoRepairExecutor(executor.config,workspace_dir=executor.workspace_dir)
        fresh.cancel(job_id=job.job_id,authority={"authority_digest":job.authority_digest})
        assert fresh._read_job_marker(job.job_id)["cancellation_requested"] is True
    result=executor.execute_job(job,before_dispatch=commit_cancel)
    assert result["status"]=="cancelled"
    assert result["manifest"]["commands"]==[] and not sentinel.exists()
    assert result["manifest"]["process_cleanup"]["oracle"]=="linux_subreaper_waitpid_echild"
    assert result["cleanup"]["cleanup_proven"] is True


@pytest.mark.parametrize("fault",["permissions","symlink","hardlink"])
def test_marker_lock_rejects_unsafe_existing_file(tmp_path,fault):
    from src.execution.repo_sandbox import LocalRepoRepairExecutor
    executor=LocalRepoRepairExecutor(RepoSandboxSettings(),workspace_dir=tmp_path)
    executor._write_job_marker("unsafe",{"status":"running"})
    path=executor._job_marker_directory/(executor._job_marker_name("unsafe")+".lock")
    if fault=="permissions":path.chmod(0o644)
    elif fault=="hardlink":os.link(path,tmp_path/"second-name")
    else:
        path.unlink();target=tmp_path/"target";target.touch(mode=0o600);path.symlink_to(target)
    with pytest.raises((RepoSandboxError,OSError)):
        with executor._job_marker_lock("unsafe"):pass
