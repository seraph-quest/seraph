from __future__ import annotations
import difflib
import errno
import hashlib
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


def test_late_cancel_preserves_committed_node_echild_terminal(tmp_path,monkeypatch):
    """Pause a genuine terminal publication before its original caller returns."""
    running=tmp_path/"running"
    body="require('node:fs').writeFileSync("+json.dumps(str(running))+",String(process.pid));setTimeout(()=>{},10000);"
    executor,repo,job=make_fixture(tmp_path,script=body)
    terminal_committed,release_owner=threading.Event(),threading.Event()
    outcomes=[];errors=[];signals=[]
    write=executor._write_job_marker
    def publish(job_id,payload):
        result=write(job_id,payload)
        if payload.get("phase")=="cleanup_verified":
            terminal_committed.set()
            assert release_owner.wait(10),"late cancellation did not release terminal owner"
        return result
    def run():
        try:outcomes.append(executor.execute_job(job))
        except BaseException as exc:errors.append(exc)
    monkeypatch.setattr(executor,"_write_job_marker",publish)
    thread=threading.Thread(target=run);thread.start()
    fresh=NodeRepoRepairExecutor(executor.config,workspace_dir=executor.workspace_dir)
    authority={"job_id":job.job_id,"authority_digest":job.authority_digest}
    try:
        deadline=time.monotonic()+10
        while not running.exists() and time.monotonic()<deadline:time.sleep(.01)
        assert running.exists()
        assert fresh.cancel(job_id=job.job_id,authority=authority)["status"]=="cancel_requested"
        assert terminal_committed.wait(10),errors
        assert thread.is_alive(),"owner returned before the terminal/cancel interleave"
        path=executor._job_marker_directory/executor._job_marker_name(job.job_id)
        original=path.read_bytes();marker=json.loads(original)
        assert marker["status"]=="cancelled" and marker["cleanup_proven"] is True
        assert marker["process_cleanup"]["process_cleanup"]["oracle"]=="linux_subreaper_waitpid_echild"
        assert supervisor.start_identity(marker["pid"]) is None
        monkeypatch.setattr(supervisor,"exact_signal",lambda *args:signals.append(args) or False)
        cancelled=fresh.cancel(job_id=job.job_id,authority=authority)
        assert path.read_bytes()==original,"late cancellation regressed the genuine ECHILD terminal"
        assert cancelled=={"status":"cancel_requested","cleanup_proven":False,"job_id":job.job_id}
        assert signals==[],"late cancellation signalled the reaped supervisor"
        recovered=fresh.reconcile(authority)
        assert recovered["status"]=="cancelled" and recovered["cleanup_proven"] is True
        assert recovered["receipt"]["stage_binding"]==marker["stage_binding"]
        assert not any((executor.workspace_dir/"artifacts/repo-sandbox/staging").iterdir())
        release_owner.set();thread.join(10)
        assert not thread.is_alive() and not errors,errors
        assert outcomes[0]["status"]=="cancelled" and outcomes[0]["cleanup"]["cleanup_proven"] is True
        assert (repo/"src/app.js").read_text()=="exports.VALUE = 1;\n"
    finally:
        release_owner.set();thread.join(10)
        assert not thread.is_alive(),"native terminal owner did not quiesce"


@pytest.mark.parametrize("boundary",["authority","attempt","fence","bool_fence","stage_binding","stage_path",
    "stage_present","stage_symlink","oracle","cleanup","terminal_hash","terminal_status","supervisor_source",
    "pid","executor_kind","valid_success","unknown_terminal","array_status","object_status","malformed","nonmapping","oversize","mode","hardlink","symlink","fifo","read_failure","named_identity"])
def test_late_node_cancel_rejects_unproven_terminal_without_write_or_signal(tmp_path,monkeypatch,boundary):
    """Synthetic invalid receipts exercise guards, not actual cleanup acceptance."""
    workspace=tmp_path/"workspace";workspace.mkdir(mode=0o700)
    executor=NodeRepoRepairExecutor(RepoSandboxSettings(profile=PROFILE),workspace_dir=workspace)
    binding={"executor_kind":"local","job_id":"terminal-node","authority_digest":"approved","attempt_id":"attempt-1","fencing_token":3}
    token=executor._stage_binding_token(binding)
    stage=executor._trusted_staging_directory()/token
    marker={"schema":"seraph.repo_repair_local_job.v1","profile":PROFILE,**binding,"base_digest":"a"*64,"posture_digest":"b"*64,
        "stage_binding":binding,"supervisor_token":token,"stage_directory":str(stage.relative_to(workspace)),
        "stage_identity":{"device":1,"inode":2},"supervisor_source_sha256":hashlib.sha256(Path(supervisor.__file__).read_bytes()).hexdigest(),
        "pid":12345,"pid_start_identity":"original-start","status":"cancelled","phase":"cleanup_verified","cleanup_proven":True,
        "process_cleanup":{"profile":PROFILE,"job_id":binding["job_id"],"token":token,"supervisor_pid":12345,
            "supervisor_start":"original-start","status":"cancelled","cleanup_proven":True,
            "process_cleanup":{"cleanup_proven":True,"oracle":"linux_subreaper_waitpid_echild"}},
        "terminal_receipt":{"status":"cancelled","manifest_sha256":"c"*64,"readback_sha256":"c"*64,"stage_binding":binding}}
    authority={key:binding[key] for key in ("job_id","authority_digest","attempt_id","fencing_token")}
    if boundary in {"authority","attempt","fence"}:
        key={"authority":"authority_digest","attempt":"attempt_id","fence":"fencing_token"}[boundary]
        authority[key]=4 if boundary=="fence" else "foreign"
    elif boundary=="bool_fence":marker["fencing_token"]=True
    elif boundary=="stage_binding":marker["stage_binding"]={**binding,"attempt_id":"foreign"}
    elif boundary=="stage_path":marker["stage_directory"]="elsewhere"
    elif boundary=="stage_present":stage.mkdir(mode=0o700)
    elif boundary=="stage_symlink":stage.symlink_to(tmp_path/"absent")
    elif boundary=="oracle":marker["process_cleanup"]["process_cleanup"]["oracle"]="not_echild"
    elif boundary=="cleanup":marker["cleanup_proven"]=False
    elif boundary=="terminal_hash":marker["terminal_receipt"]["readback_sha256"]="d"*64
    elif boundary=="terminal_status":marker["status"]="succeeded"
    elif boundary=="array_status":marker["status"]=[]
    elif boundary=="object_status":marker["status"]={}
    elif boundary=="supervisor_source":marker["supervisor_source_sha256"]="e"*64
    elif boundary=="pid":marker["pid"]=True
    elif boundary=="executor_kind":marker["executor_kind"]="foreign"
    elif boundary in {"valid_success","unknown_terminal"}:
        status="succeeded" if boundary=="valid_success" else "unknown_external_effect"
        marker["status"]=marker["terminal_receipt"]["status"]=marker["process_cleanup"]["status"]=status
        if boundary=="unknown_terminal":
            marker["cleanup_proven"]=False
            marker["phase"]="worker_started"
            marker["process_cleanup"]["cleanup_proven"]=False
            marker["process_cleanup"]["process_cleanup"]["cleanup_proven"]=False
    executor._write_job_marker(binding["job_id"],marker)
    path=executor._job_marker_directory/executor._job_marker_name(binding["job_id"])
    if boundary=="malformed":path.write_bytes(b"not-json")
    elif boundary=="nonmapping":path.write_bytes(b"[]")
    elif boundary=="oversize":
        with path.open('ab') as stream:stream.write(b" "*(16*1024))
    elif boundary=="mode":path.chmod(0o644)
    elif boundary=="hardlink":os.link(path,tmp_path/"second-name")
    elif boundary in {"symlink","fifo"}:
        path.unlink()
        if boundary=="symlink":path.symlink_to(tmp_path/"absent")
        else:os.mkfifo(path,mode=0o600)
    original=path.read_bytes() if boundary not in {"symlink","fifo"} else None
    identity=path.lstat()
    if boundary=="read_failure":
        def fail_read(*args):raise OSError("fixture marker unavailable")
        monkeypatch.setattr(os,"read",fail_read)
    elif boundary=="named_identity":
        other=tmp_path/"other-marker";other.write_bytes(b"{}");other.chmod(0o600)
        other_metadata=other.stat();original_stat=os.stat
        def changed_name(name,*args,**kwargs):
            if name==path.name and "dir_fd" in kwargs:return other_metadata
            return original_stat(name,*args,**kwargs)
        monkeypatch.setattr(os,"stat",changed_name)
    signals=[];monkeypatch.setattr(supervisor,"exact_signal",lambda *args:signals.append(args) or True)
    result=executor.cancel(job_id=binding["job_id"],authority=authority)
    assert result["status"]=="unknown_external_effect" and result["cleanup_proven"] is False
    assert signals==[]
    if original is not None:assert path.read_bytes()==original
    else:
        current=path.lstat();assert (current.st_dev,current.st_ino,current.st_mode)==(identity.st_dev,identity.st_ino,identity.st_mode)


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
