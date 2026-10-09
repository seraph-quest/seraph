"""Exclusive Linux supervisor for fixed Node and iterative Python repair jobs.

Not a sandbox. Never enable subreaping in the multithreaded Seraph backend.
The executable entrypoint accepts only a private fixed-profile job and a
durably acknowledged start token; no public generic execution API exists.
"""
from __future__ import annotations
import ctypes
import hashlib
import json
import os
from pathlib import Path
import selectors
import signal
import stat
import subprocess
import sys
import time
from typing import Any

WAIT_ALL = 0x40000000
MAX_CHILDREN = 1024
MAX_CHILD_READ = 16 * 1024
CANCELLED = False
PYTHON_PROFILE = "repo-python-pytest-iterative-supervisor-v1"


def finish_supervisor(process: subprocess.Popen, *, deadline: float, stream_limit: int) -> dict[str, Any]:
    """Witness complete bounded output EOF, descriptor closure and actual wait.

    A timeout leaves closure unproven. Closing a live pipe or missing PID never
    substitutes for EOF or for the original supervisor's ECHILD receipt.
    """
    if process.stdin is None or not process.stdin.closed:
        raise ValueError("supervisor_stdin_closure_unproven")
    selector = selectors.DefaultSelector()
    buffers = {"stdout": bytearray(), "stderr": bytearray()}
    overflow = False
    streams = {"stdout": process.stdout, "stderr": process.stderr}
    try:
        for name, stream in streams.items():
            if stream is None or stream.closed:
                raise ValueError("supervisor_original_output_missing")
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, selectors.EVENT_READ, name)
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ValueError("supervisor_output_eof_unproven")
            for event, _ in selector.select(timeout=min(.02, remaining)):
                data = os.read(event.fileobj.fileno(), 65536)
                if not data:
                    selector.unregister(event.fileobj)
                    event.fileobj.close()
                    continue
                target = buffers[event.data]
                room = max(0, stream_limit - len(target))
                overflow = overflow or len(data) > room
                target.extend(data[:room])
        code = process.wait(timeout=max(0, deadline - time.monotonic()))
        if overflow:
            raise ValueError("supervisor_output_limit")
        return {"stdin_closed": True, "stdout_eof": True, "stderr_eof": True,
                "stdout_closed": process.stdout.closed, "stderr_closed": process.stderr.closed,
                "waited": True, "returncode": code,
                "stdout_sha256": hashlib.sha256(buffers["stdout"]).hexdigest(),
                "stderr_sha256": hashlib.sha256(buffers["stderr"]).hexdigest()}
    finally:
        selector.close()
def linux_syscall() -> Any:
    # Resolve Linux-only symbols only after selecting this optional executor.
    # Module import and default Python/core startup remain portable.
    if sys.platform != "linux" or os.uname().machine != "x86_64" or ctypes.sizeof(ctypes.c_void_p) != 8:
        raise ValueError("node_linux_supervisor_unavailable")
    library = ctypes.CDLL(None, use_errno=True)
    try:
        operation = library.syscall
    except AttributeError as exc:
        raise ValueError("node_linux_syscall_unavailable") from exc
    operation.restype = ctypes.c_long
    return operation


def pidfd_open(pid: int) -> int:
    descriptor = int(linux_syscall()(ctypes.c_long(434), ctypes.c_int(pid), ctypes.c_uint(0)))
    if descriptor < 0:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code))
    return descriptor


def pidfd_send(descriptor: int, signum: int) -> None:
    result = linux_syscall()(ctypes.c_long(424), ctypes.c_int(descriptor), ctypes.c_int(signum), ctypes.c_void_p(None), ctypes.c_uint(0))
    if result < 0:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code))


def platform_ready() -> None:
    # Verified Linux x86_64 syscall table only; other architectures block.
    # uv's portable Python may omit the Python pidfd conveniences even when
    # the kernel implements the required process-descriptor operations.
    if sys.platform != "linux" or os.uname().machine != "x86_64" or ctypes.sizeof(ctypes.c_void_p) != 8:
        raise ValueError("node_linux_supervisor_unavailable")
    if not Path(f"/proc/{os.getpid()}/task/{os.getpid()}/children").exists():
        raise ValueError("node_linux_ancestry_inspection_unavailable")
    descriptor=pidfd_open(os.getpid())
    try:pidfd_send(descriptor,0)
    finally:os.close(descriptor)


def enable_subreaper() -> None:
    platform_ready()
    signal.signal(signal.SIGCHLD,signal.SIG_DFL)
    if signal.getsignal(signal.SIGCHLD)!=signal.SIG_DFL:
        raise ValueError("node_sigchld_default_unavailable")
    libc=ctypes.CDLL(None,use_errno=True)
    libc.prctl.argtypes=[ctypes.c_int,ctypes.c_ulong,ctypes.c_ulong,ctypes.c_ulong,ctypes.c_ulong]
    libc.prctl.restype=ctypes.c_int
    value=ctypes.c_int()
    if libc.prctl(36,1,0,0,0)!=0 or libc.prctl(37,ctypes.addressof(value),0,0,0)!=0 or value.value!=1:
        raise ValueError("node_subreaper_unavailable")


def require_subreaper() -> None:
    platform_ready()
    value = ctypes.c_int()
    libc = ctypes.CDLL(None, use_errno=True)
    libc.prctl.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong]
    libc.prctl.restype = ctypes.c_int
    if libc.prctl(37, ctypes.addressof(value), 0, 0, 0) != 0 or value.value != 1:
        raise ValueError("dedicated_subreaper_required")


def start_identity(pid: int) -> str | None:
    try:
        descriptor=os.open(f"/proc/{pid}/stat",os.O_RDONLY|os.O_CLOEXEC)
        try:raw=os.read(descriptor,4096).decode()
        finally:os.close(descriptor)
        return raw.rsplit(")",1)[1].split()[19]
    except (OSError,ValueError,IndexError):
        return None


def exact_signal(pid: int, start: str, signum: int) -> bool:
    if pid<=0 or not start or start_identity(pid)!=start:return False
    try:
        descriptor=pidfd_open(pid)
        try:
            if start_identity(pid)!=start:return False
            pidfd_send(descriptor,signum)
            return True
        finally:os.close(descriptor)
    except ProcessLookupError:return False


def owned_children() -> list[int]:
    with Path(f"/proc/{os.getpid()}/task/{os.getpid()}/children").open("rb") as handle:
        raw=handle.read(MAX_CHILD_READ+1)
    if len(raw)>MAX_CHILD_READ:raise ValueError("node_child_enumeration_limit")
    result=[int(value) for value in raw.split()]
    if len(result)>MAX_CHILDREN:raise ValueError("node_child_enumeration_limit")
    return result


def cleanup(deadline: float) -> dict[str,Any]:
    reaped=0;signalled=0;wait_statuses={}
    while time.monotonic()<deadline:
        for pid in owned_children():
            start=start_identity(pid)
            if start is None:
                continue # final wait oracle, never absence of a scan, decides
            if exact_signal(pid,start,signal.SIGKILL):signalled+=1
        while True:
            try:pid,_status=os.waitpid(-1,os.WNOHANG|WAIT_ALL)
            except ChildProcessError:
                return {"cleanup_proven":True,"oracle":"linux_subreaper_waitpid_echild","reaped":reaped,"signalled":signalled,"wait_statuses":wait_statuses}
            if pid==0:break
            reaped+=1
            wait_statuses[str(pid)] = _status
        time.sleep(min(.005,max(0,deadline-time.monotonic())))
    return {"cleanup_proven":False,"reason":"node_cleanup_deadline_exhausted","reaped":reaped,"signalled":signalled}


def cancel(signum: int, frame: Any) -> None:
    global CANCELLED
    CANCELLED=True


def run_command(argv: list[str], cwd: Path, env: dict[str,str], deadline: float, *, stream_limit: int) -> dict[str,Any]:
    if CANCELLED:raise ValueError("node_cancelled_before_command")
    process=subprocess.Popen(argv,cwd=cwd,env=env,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE,start_new_session=True)
    selector=selectors.DefaultSelector();buffers={"stdout":bytearray(),"stderr":bytearray()};truncated=False
    for name,stream in (("stdout",process.stdout),("stderr",process.stderr)):
        os.set_blocking(stream.fileno(),False);selector.register(stream,selectors.EVENT_READ,name)
    command_deadline=deadline-min(1.0,max(.1,(deadline-time.monotonic())*.1))
    timed_out=False;code=None;leftover=False;proof=None
    try:
        while selector.get_map() or code is None:
            code=process.poll()
            if proof is None and (code is not None or CANCELLED or time.monotonic()>=command_deadline):
                timed_out=code is None and not CANCELLED
                leftover=code is not None and bool(owned_children())
                proof=cleanup(deadline)
                if not proof["cleanup_proven"]:raise ValueError("node_cleanup_unproven")
                if code is None:
                    status = proof.get("wait_statuses", {}).get(str(process.pid))
                    if status is None:
                        raise ValueError("command_original_wait_status_missing")
                    code = os.waitstatus_to_exitcode(status)
                process.returncode=code
            if time.monotonic()>=deadline:raise ValueError("node_output_deadline_exhausted")
            for event,_ in selector.select(timeout=min(.01,max(0,deadline-time.monotonic()))):
                data=os.read(event.fileobj.fileno(),65536)
                if not data:selector.unregister(event.fileobj);continue
                target=buffers[event.data];room=stream_limit-len(target)
                if len(data)>room:truncated=True
                target.extend(data[:max(0,room)])
        process.wait(timeout=max(0, deadline-time.monotonic()))
        return {"argv":argv,"exit_code":code,"timed_out":timed_out,"cancelled":CANCELLED,"leftover_descendant":leftover,
                "stdout_eof":True,"stderr_eof":True,"waited":True,
                "stdout_truncated":truncated,"stdout":bytes(buffers["stdout"]),"stderr":bytes(buffers["stderr"]),"cleanup":proof}
    finally:
        selector.close()
        process.stdout.close();process.stderr.close()


def run_python(job: dict[str, Any]) -> int:
    """Second fixed dispatch: pinned worker, interpreter and scalar iteration."""
    global CANCELLED
    from src.execution import repo_worker
    stage = Path(job["stage"])
    output = stage / "out"
    deadline = float(job["deadline_at"])
    token = job["token"]
    identity = start_identity(os.getpid())
    runtime = job["runtime"]
    if (runtime.get("interpreter_entry_path") != str(Path(sys.executable).absolute())
        or runtime.get("interpreter_sha256") != hashlib.sha256(Path(sys.executable).resolve().read_bytes()).hexdigest()
        or runtime.get("worker_source_sha256") != hashlib.sha256(Path(repo_worker.__file__).read_bytes()).hexdigest()
        or job.get("supervisor_source_sha256") != hashlib.sha256(Path(__file__).read_bytes()).hexdigest()):
        raise ValueError("python_supervisor_runtime_changed")
    selector = selectors.DefaultSelector()
    selector.register(sys.stdin, selectors.EVENT_READ)
    try:
        while not CANCELLED and time.monotonic() < deadline:
            if selector.select(timeout=.01):
                received = sys.stdin.buffer.readline(256).decode().strip()
                if received == "cancel:" + token:
                    CANCELLED = True
                elif received != token:
                    raise ValueError("python_dispatch_barrier_missing")
                break
        else:
            CANCELLED = True
    finally:
        selector.close()
    commands = []
    def command(argv, *, cwd, environment, deadline_at, timeout):
        if CANCELLED:
            raise repo_worker.WorkerInputError("python_cancelled_before_command")
        _package_path, package_digest = repo_worker._pytest_package_identity()
        if (runtime["interpreter_sha256"] != hashlib.sha256(Path(sys.executable).resolve().read_bytes()).hexdigest()
            or runtime["worker_source_sha256"] != hashlib.sha256(Path(repo_worker.__file__).read_bytes()).hexdigest()
            or runtime["pytest_package_sha256"] != package_digest
            or job["supervisor_source_sha256"] != hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
            or job["git_sha256"] != hashlib.sha256(Path("/usr/bin/git").resolve().read_bytes()).hexdigest()):
            raise repo_worker.WorkerInputError("python_runtime_changed_before_command")
        if argv[0] == "/usr/bin/git":
            argv = [argv[0], "-c", "maintenance.auto=false", "-c", "gc.auto=0", *argv[1:]]
        result = run_command(argv, cwd, environment, min(deadline, float(deadline_at), time.monotonic() + timeout), stream_limit=repo_worker.MAX_STREAM_BYTES)
        commands.append({key: value for key, value in result.items() if key not in {"stdout", "stderr"}} | {
            "stdout_sha256": hashlib.sha256(result["stdout"]).hexdigest(), "stderr_sha256": hashlib.sha256(result["stderr"]).hexdigest()})
        if result["leftover_descendant"] or result["stdout_truncated"] or result["cancelled"]:
            raise repo_worker.WorkerInputError("python_command_quiescence_invalid")
        return result["exit_code"], result["stdout"], result["stderr"], result["timed_out"]
    worker_exit = None
    if not CANCELLED:
        worker_exit = repo_worker.run_supervised_local_job(stage / "input" / "job.json",
            command_runner=command, workspace_root=stage / "workspace", output_root=output,
            pytest_executable=runtime["pytest_executable_path"], environment=job["environment"],
            deadline_at=deadline, expected_identity={key: runtime[key] for key in (
                "worker_source_sha256", "interpreter_sha256", "pytest_executable_sha256", "pytest_package_sha256")})
    if CANCELLED and worker_exit is None:
        blocked = json.dumps({"profile": repo_worker.PROFILE, "status": "blocked", "reason": "python_cancelled_before_dispatch"}).encode()
        for name in ("manifest.json", "readback.json"):
            repo_worker._write_private_output(output, name, blocked)
        for name in ("diff.patch", "pytest.stdout", "pytest.stderr"):
            repo_worker._write_private_output(output, name, b"")
    proof = cleanup(deadline)
    result = {"profile": PYTHON_PROFILE, "job_id": job["job_id"], "iteration_binding": job["iteration_binding"],
              "token": token, "supervisor_pid": os.getpid(), "supervisor_start": identity,
              "status": "cancelled" if CANCELLED else "succeeded" if worker_exit == 0 else "failed",
              "worker_exit": worker_exit, "commands": commands,
              "cleanup_proven": proof["cleanup_proven"], "process_cleanup": proof}
    repo_worker._write_private_output(output, "supervisor-result.json", json.dumps(result, sort_keys=True).encode())
    return 0 if proof["cleanup_proven"] else 2


def main(request_file: Path) -> int:
    global CANCELLED
    enable_subreaper()
    signal.signal(signal.SIGTERM,cancel)
    signal.signal(signal.SIGINT,cancel)
    # Insert only the server-owned backend, never the staged repository.
    sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
    from src.execution.repo_node import PROFILE, execution_plan, hash_file, immutable, read_regular, worker_sources_digest, NodeRepoRepairExecutor
    from src.execution.repo_sandbox import _patch_paths_from_diff, _digest_entries, SnapshotEntry
    from src.execution.repo_worker import _write_private_output
    descriptor=os.open(request_file,os.O_RDONLY|os.O_NOFOLLOW|os.O_CLOEXEC)
    try:
        metadata=os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid!=os.getuid() or metadata.st_size>32768:
            raise ValueError("node_supervisor_request_untrusted")
        job=json.loads(os.read(descriptor,32769))
    finally:os.close(descriptor)
    if job.get("profile") == PYTHON_PROFILE:
        return run_python(job)
    if job.get("profile")!=PROFILE:raise ValueError("node_supervisor_profile_invalid")
    stage=Path(job["stage"]);workspace=stage/"workspace";output=stage/"out";deadline=float(job["deadline_at"])
    token=job["token"];identity=start_identity(os.getpid())
    selector=selectors.DefaultSelector();selector.register(sys.stdin,selectors.EVENT_READ)
    try:
        while not CANCELLED and time.monotonic()<deadline:
            if selector.select(timeout=.01):
                received=sys.stdin.buffer.readline(256).decode().strip()
                if received=="cancel:"+token:
                    CANCELLED=True
                elif received!=token:
                    raise ValueError("node_dispatch_barrier_missing")
                break
        else:
            CANCELLED=True
    finally:selector.close()
    if CANCELLED:
        proof=cleanup(deadline)
        for name in ("diff.patch","pytest.stdout","pytest.stderr","build.stdout","build.stderr"):
            _write_private_output(output,name,b"")
        result={"profile":PROFILE,"job_id":job["job_id"],"token":token,"supervisor_pid":os.getpid(),"supervisor_start":identity,
                "status":"cancelled","reason":"node_cancelled_before_dispatch","commands":[],
                "diff_sha256":hashlib.sha256(b"").hexdigest(),"cleanup_proven":proof["cleanup_proven"],"process_cleanup":proof}
        _write_private_output(output,"supervisor-result.json",json.dumps(result,sort_keys=True).encode())
        return 0 if proof["cleanup_proven"] else 2
    runtime=job["runtime"]
    node=Path(runtime["node_path"])
    npm=node.parent.parent/"lib/node_modules/npm"
    checked={"node_sha256":hash_file(node),"supervisor_source_sha256":hash_file(Path(__file__)),
             "worker_source_sha256":worker_sources_digest(),"git_sha256":hash_file(Path("/usr/bin/git").resolve()),
             "interpreter_sha256":hash_file(Path(sys.executable).resolve()),
             "npm_package_sha256":hash_file(npm/"package.json"),"npm_entry_sha256":hash_file(npm/"bin/npm-cli.js")}
    if runtime.get("interpreter_entry_path")!=str(Path(sys.executable).absolute()) or any(runtime.get(key)!=value for key,value in checked.items()):
        raise ValueError("node_toolchain_drift_before_dispatch")
    plan=execution_plan(workspace,job["plan"]["selection"],job["allowed_paths"],runtime)
    for key,value in plan.items():
        if job["plan"].get(key)!=value:raise ValueError("node_execution_plan_drift")
    patch=read_regular(stage,"patch.diff",job["limits"]["max_patch_bytes"])
    patch_paths=_patch_paths_from_diff(patch,job["allowed_paths"])
    if any(immutable(path) for path in patch_paths):raise ValueError("node_immutable_patch_target")
    env=job["environment"];commands=[];reason=None;snapshot=None
    def command(argv: list[str]) -> dict[str,Any]:
        result=run_command(argv,workspace,env,deadline,stream_limit=job["limits"]["max_stream_bytes"])
        if result["cancelled"] or result["timed_out"] or result["leftover_descendant"] or result["stdout_truncated"]:
            raise ValueError("node_command_cancelled_or_timeout_or_descendant")
        return result
    try:
        for args in (["init","--initial-branch=main"],["config","user.email","seraph-worker@localhost"],["config","user.name","Seraph worker"],["add","--all"],["commit","--allow-empty","-m","seraph snapshot"],["apply","--check",str(stage/"patch.diff")],["apply","--whitespace=nowarn",str(stage/"patch.diff")]):
            # Temporary bootstrap Git must not spawn detached maintenance;
            # every descendant remains subject to the strict cleanup oracle.
            result=command(["/usr/bin/git","-c","maintenance.auto=false","-c","gc.auto=0",*args])
            if result["exit_code"]!=0:raise ValueError("node_git_stage_failed")
        before_inputs={path:hash_file(workspace/path) for path in ("package.json","package-lock.json")}
        for entry in plan["commands"]:
            # Generated test paths must now exist as bounded regular files.
            for path in entry["paths"]:read_regular(workspace,path,job["limits"]["max_file_bytes"])
            result=run_command(entry["argv"],workspace,env,deadline,stream_limit=job["limits"]["max_stream_bytes"])
            commands.append({key:value for key,value in result.items() if key not in {"stdout","stderr"}}|{"script":entry["script"],"stdout_sha256":hashlib.sha256(result["stdout"]).hexdigest(),"stderr_sha256":hashlib.sha256(result["stderr"]).hexdigest()})
            _write_private_output(output,"pytest.stdout" if entry["script"]=="test" else "build.stdout",result["stdout"])
            _write_private_output(output,"pytest.stderr" if entry["script"]=="test" else "build.stderr",result["stderr"])
            if result["cancelled"] or result["timed_out"] or result["leftover_descendant"] or result["stdout_truncated"] or result["exit_code"]!=0:
                reason="node_command_failed_or_cancelled_or_timeout_or_descendant";break
        if reason and not job.get("iteration_binding"):
            raise ValueError(reason)
        if any(hash_file(workspace/path)!=value for path,value in before_inputs.items()):raise ValueError("node_immutable_input_changed")
        output_dir=plan["output_directory"]
        # Validate every output path and byte through descriptor-safe snapshot;
        # dependency/config changes cannot be adopted as source patch evidence.
        from config.settings import RepoSandboxSettings
        executor=NodeRepoRepairExecutor(RepoSandboxSettings(profile=PROFILE,node_runtime_path=runtime["node_path"]),workspace_dir=stage)
        snapshot=executor.snapshot_repository(workspace,stage/"after-check")
        total_output=sum(entry.size_bytes for entry in snapshot.entries if output_dir and entry.relative_path.startswith(output_dir+"/"))
        if total_output>job["limits"]["max_output_bytes"]:raise ValueError("node_generated_output_limit")
        current_plan=execution_plan(workspace,plan["selection"],job["allowed_paths"],runtime)
        if current_plan!=plan:raise ValueError("node_frozen_execution_input_drift")
        dependencies=[entry for entry in snapshot.entries if entry.relative_path.startswith("node_modules/")]
        from dataclasses import asdict
        from src.execution.repo_sandbox import executor_posture_digest
        if executor_posture_digest({"entries":[{"relative_path":entry.relative_path,"size_bytes":entry.size_bytes,"sha256":entry.sha256} for entry in dependencies]})!=job["plan"]["dependency_manifest_sha256"]:
            raise ValueError("node_dependency_drift_after_execution")
        command(["/usr/bin/git","add","--all"])
        names=command(["/usr/bin/git","diff","--cached","--name-only","-z","--no-renames","--no-ext-diff","--no-color"])["stdout"]
        paths=[name.decode() for name in names.rstrip(b"\0").split(b"\0") if name]
        generated=[path for path in paths if output_dir and path.startswith(output_dir+"/")]
        changed=[path for path in paths if path not in generated]
        if not set(changed).issubset(job["allowed_paths"]) or not set(patch_paths).issubset(changed):raise ValueError("node_unapproved_diff_path")
        if generated:
            command(["/usr/bin/git","reset","--",*generated])
        diff=command(["/usr/bin/git","diff","--cached","--binary","--no-ext-diff","--no-color"])["stdout"]
        if len(diff)>job["limits"]["max_output_bytes"]:raise ValueError("node_diff_output_limit")
        _write_private_output(output,"diff.patch",diff)
    except (ValueError,OSError) as exc:
        reason=str(exc)[:512]
    finally:
        proof=cleanup(deadline)
    for name in ("diff.patch","pytest.stdout","pytest.stderr","build.stdout","build.stderr"):
        if not (output/name).exists():_write_private_output(output,name,b"")
    result={"profile":PROFILE,"job_id":job["job_id"],"token":token,"supervisor_pid":os.getpid(),"supervisor_start":identity,
            "status":"cancelled" if CANCELLED else "failed" if reason else "succeeded","reason":reason,"commands":commands,
            "diff_sha256":hash_file(output/"diff.patch"),"cleanup_proven":proof["cleanup_proven"],"process_cleanup":proof,
            **({"iteration_binding":job["iteration_binding"]} if job.get("iteration_binding") else {})}
    if job.get("iteration_binding") and snapshot is not None:
        result.update(after_digest=snapshot.digest, tested_file_hash_metadata=[
            {"path": entry.relative_path, "size_bytes": entry.size_bytes, "sha256": entry.sha256}
            for entry in snapshot.entries])
        if len(json.dumps(result, sort_keys=True).encode()) > job["limits"]["max_output_bytes"]:
            raise ValueError("node_iterative_readback_metadata_limit")
    _write_private_output(output,"supervisor-result.json",json.dumps(result,sort_keys=True).encode())
    return 0 if proof["cleanup_proven"] else 2


if __name__=="__main__":
    if len(sys.argv)==2 and sys.argv[1]=="--probe":
        enable_subreaper();print("linux_subreaper_ready");raise SystemExit(0)
    if len(sys.argv)!=2:raise SystemExit(2)
    try:raise SystemExit(main(Path(sys.argv[1])))
    except Exception as exc:
        print(type(exc).__name__+": "+str(exc)[:256],file=sys.stderr)
        raise SystemExit(2)
