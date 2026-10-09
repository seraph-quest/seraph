"""One frozen Node/npm-script profile behind the existing repair executor.

No npm, shell, installation or generic command interface is exposed. Local
execution is host-user execution; process supervision is not OS isolation.
"""
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import signal
import stat
import subprocess
import sys
import time
from typing import Any, Callable, Mapping

from src.execution.repo_sandbox import (
    LocalRepoRepairExecutor, RepoSandboxError, RepoSandboxJob, RepoSandboxPreflight,
    RepositorySnapshot, SnapshotEntry, _digest_entries, _open_source_regular_file,
    _assert_stable_file, _patch_paths_from_diff, executor_posture_digest, limits_digest,
    _open_trusted_directory, _same_file_metadata,
)

PROFILE = "repo-node24-npm-v1"
MAX_DEP_FILES = 512
MAX_DEP_FILE_BYTES = 16 * 1024 * 1024
MAX_METADATA_BYTES = 2 * 1024 * 1024
SELECTIONS = {( "npm", "test"): ("test",), ("npm", "run", "build"): ("build",), ("npm", "run", "build", "test"): ("build", "test")}
PATH_TOKEN = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_./-]{0,511}$")


def normalize_selection(arguments: Any) -> tuple[str, ...]:
    selected = tuple(arguments)
    if selected not in SELECTIONS:
        raise RepoSandboxError("Node selection must be npm test, npm run build, or npm run build test")
    return selected


def relative(value: str) -> str:
    if not PATH_TOKEN.fullmatch(value) or any(part in {"", ".", ".."} for part in value.split("/")):
        raise RepoSandboxError("Node script path is not a finite ASCII relative path")
    return value


def immutable(path: str) -> bool:
    return path in {"package.json", "package-lock.json"} or path.endswith(".json") or path.startswith("node_modules/")


def read_regular(root: Path, name: str, limit: int = MAX_METADATA_BYTES) -> bytes:
    return _read_regular_with_metadata(root, name, limit)[0]


def _read_regular_with_metadata(root: Path, name: str, limit: int = MAX_METADATA_BYTES):
    descriptor, metadata = _open_source_regular_file(root, name)
    try:
        if metadata.st_size > limit or metadata.st_nlink != 1:
            raise RepoSandboxError("Node input exceeds its regular-file bound")
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = -1
            data = handle.read(limit + 1)
            _assert_stable_file(metadata, os.fstat(handle.fileno()))
        if len(data) > limit:
            raise RepoSandboxError("Node input grew beyond its bound")
        return data, metadata
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def json_regular(root: Path, name: str) -> dict[str, Any]:
    try:
        value = json.loads(read_regular(root, name))
    except (OSError, ValueError) as exc:
        raise RepoSandboxError("Node JSON execution input is unavailable or malformed") from exc
    if not isinstance(value, dict):
        raise RepoSandboxError("Node execution input must be a JSON object")
    return value


def hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def worker_sources_digest() -> str:
    """Bind the trusted Node adapter and its shared stage/output helpers."""
    directory = Path(__file__).parent
    parts = [(name, hash_file(directory / name)) for name in ("repo_node.py", "repo_worker.py", "repo_sandbox.py")]
    return hashlib.sha256(json.dumps(parts, separators=(",", ":")).encode()).hexdigest()


def runtime_identity(config: Any) -> dict[str, Any]:
    from src.execution.repo_supervisor import platform_ready
    platform_ready()
    node = Path(config.node_runtime_path)
    if not node.is_absolute() or node.is_symlink():
        raise RepoSandboxError("Node runtime requires a server-selected absolute regular executable")
    metadata = node.stat()
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1 or metadata.st_mode & 0o022 or not os.access(node, os.X_OK):
        raise RepoSandboxError("Node runtime is not a trusted installed executable")
    before = hash_file(node)
    version = subprocess.run([str(node), "--version"], check=True, capture_output=True, timeout=2, env={"PATH":"/usr/bin:/bin", "LANG":"C.UTF-8"}).stdout.decode().strip()
    if not re.fullmatch(r"v24\.\d+\.\d+", version) or hash_file(node) != before:
        raise RepoSandboxError("Node runtime must be an unchanged installed Node 24")
    npm_root = node.parent.parent / "lib/node_modules/npm"
    npm = json_regular(npm_root, "package.json")
    npm_entry = npm_root / "bin/npm-cli.js"
    interpreter = Path(sys.executable).absolute()
    supervisor = Path(__file__).with_name("repo_supervisor.py")
    subprocess.run([str(interpreter), "-I", str(supervisor), "--probe"],check=True,capture_output=True,timeout=2,env={"PATH":"/usr/bin:/bin"})
    return {
        "node_path":str(node), "node_version":version, "node_sha256":before,
        "npm_version":npm.get("version"), "npm_package_sha256":hash_file(npm_root/"package.json"),
        "npm_entry_sha256":hash_file(npm_entry), "npm_executed":False,
        "interpreter_entry_path":str(interpreter), "interpreter_sha256":hash_file(interpreter.resolve()),
        "worker_source_sha256":worker_sources_digest(), "supervisor_source_sha256":hash_file(supervisor),
        "git_sha256":hash_file(Path("/usr/bin/git").resolve()),
    }


def canonical_node_command(name, body, node_path):
    """Pure canonical builder for exactly the reviewed direct-command grammar.

    Proposed shared use by execution_plan and the durable sanitizer. This
    function does not read files or execute commands; the planner separately
    verifies selected paths, immutable config and installed compiler bytes.
    """
    from src.execution.repo_node import relative
    from src.execution.repo_sandbox import RepoSandboxError

    if name not in {"test", "build"} or not isinstance(body, str) or not 0 < len(body) <= 4096:
        raise RepoSandboxError("Node script metadata is outside finite grammar")
    tokens = body.split(" ")
    if any(not token for token in tokens) or any("\x00" in token for token in tokens):
        raise RepoSandboxError("Node script requires exact single-space tokens")
    config = None
    if name == "test" and tokens[:2] == ["node", "--test"] and 1 <= len(tokens[2:]) <= 8:
        paths = [relative(value) for value in tokens[2:]]
        if any(not value.endswith((".test.js", ".test.mjs")) for value in paths):
            raise RepoSandboxError("Node tests require finite test.js/test.mjs paths")
        argv = [node_path, "--test", *paths]
    elif tokens[:1] == ["node"] and len(tokens) == 2:
        paths = [relative(tokens[1])]
        if not paths[0].endswith((".js", ".mjs", ".cjs")):
            raise RepoSandboxError("Node entrypoint requires finite JavaScript path")
        argv = [node_path, *paths]
    elif name == "build" and len(tokens) == 3 and tokens[:2] == ["tsc", "--project"]:
        config = relative(tokens[2])
        if not config.endswith(".json"):
            raise RepoSandboxError("TypeScript requires finite inspected JSON config")
        argv = [node_path, "node_modules/typescript/lib/tsc.js", "--project", config]
        paths = []
    else:
        raise RepoSandboxError("Node metadata is outside approved direct-command grammar")
    return {"script": name, "body": body, "argv": argv, "paths": paths}, config


def safe_node_posture(authority, *, expected_posture=None):
    """Preserve metadata only against independently inspected server facts.

    The expectation must be an independent copy of the selected executor's
    actual fresh preflight.posture, supplied by the trusted admission/claim
    seam. It must never be extracted from authority or its digest. This pure
    validator checks shape and equality; it does not attest the host itself.
    """
    from src.execution.repo_node import PROFILE, relative, SELECTIONS
    from src.execution.repo_sandbox import RepoSandboxError

    posture = authority.get("executor_posture")
    if authority.get("sandbox_profile") != PROFILE or authority.get("executor_profile") != "local:" + PROFILE or authority.get("executor_kind") != "local":
        return None
    if authority.get("required_permissions") != ["local_host_execution"] or authority.get("local_host_execution_required") is not True:
        return None
    if not isinstance(posture, dict):
        return None
    if not isinstance(expected_posture, dict):
        return None
    # The trusted snapshot must not alias mutable objects in the candidate.
    # An independent JSON copy at the server seam prevents mutations of a
    # proposed authority from changing its own expectation.
    def mutable_ids(value, depth=0):
        if depth > 8:
            raise ValueError("Node posture nesting exceeded")
        if isinstance(value, dict):
            if len(value) > 64: raise ValueError("Node posture fields exceeded")
            return {id(value)} | set().union(*(mutable_ids(item, depth + 1) for item in value.values()))
        if isinstance(value, list):
            if len(value) > 16: raise ValueError("Node posture sequence exceeded")
            return {id(value)} | set().union(*(mutable_ids(item, depth + 1) for item in value))
        return set()
    try:
        if mutable_ids(posture) & mutable_ids(expected_posture):
            return None
    except ValueError:
        return None
    digest_fields = {
        "limits_digest", "node_sha256", "npm_package_sha256", "npm_entry_sha256",
        "interpreter_sha256", "worker_source_sha256", "supervisor_source_sha256", "git_sha256",
    }
    string_fields = {"node_path", "node_version", "npm_version", "interpreter_entry_path"}
    fixed = {"kind": "local", "profile": PROFILE, "isolation_claim": "none",
             "network_isolation": "not_verified", "resource_enforcement": "admission_and_wall_timeout_only",
             "host_access": "explicit_job_approval_required", "process_supervision": "linux_per_job_subreaper",
             "npm_executed": False}
    allowed = digest_fields | string_fields | set(fixed) | {"unenforced_limits", "dependency_limits", "execution_plan"}
    required = allowed - {"execution_plan"}
    def digest(value):
        return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None
    def text(value, bound=512):
        return isinstance(value, str) and 0 < len(value) <= bound and "\x00" not in value
    def path(value):
        try:
            return text(value) and relative(value) == value
        except RepoSandboxError:
            return False
    if set(posture) - allowed or not required <= set(posture):
        return None
    if any(type(posture[key]) is not type(value) or posture[key] != value for key, value in fixed.items()):
        return None
    if any(not digest(posture[key]) for key in digest_fields) or any(not text(posture[key]) for key in string_fields):
        return None
    if posture["limits_digest"] != authority.get("sandbox_limits_digest"):
        return None
    if not re.fullmatch(r"v24\.\d+\.\d+", posture["node_version"]):
        return None
    if not re.fullmatch(r"\d+\.\d+\.\d+", posture["npm_version"]):
        return None
    if not posture["node_path"].startswith("/") or not posture["interpreter_entry_path"].startswith("/"):
        return None
    if posture["unenforced_limits"] != ["cpu", "memory", "pids"]:
        return None
    limits = posture["dependency_limits"]
    if limits != {"files": 512, "file_bytes": 16777216, "aggregate_bytes": 67108864} or any(type(value) is not int for value in limits.values()):
        return None
    plan = posture.get("execution_plan")
    if plan is not None:
        expected = {"selection", "commands", "output_directory", "package_sha256", "lockfile_sha256", "configs",
                    "lifecycle_hooks_executed", "dependency_manifest_sha256", "dependency_aliases", "snapshot_sha256"}
        if not isinstance(plan, dict) or set(plan) != expected or plan["lifecycle_hooks_executed"] is not False:
            return None
        selection = plan["selection"]
        if not isinstance(selection, list) or any(not isinstance(item, str) for item in selection) or tuple(selection) not in SELECTIONS:
            return None
        commands = plan["commands"]
        if not isinstance(commands, list) or len(commands) != len(SELECTIONS[tuple(selection)]):
            return None
        compiler_config = None
        for name, command in zip(SELECTIONS[tuple(selection)], commands):
            if not isinstance(command, dict) or set(command) != {"script", "body", "argv", "paths"} or command["script"] != name or not text(command["body"], 4096):
                return None
            argv, paths = command["argv"], command["paths"]
            if not isinstance(argv, list) or not 2 <= len(argv) <= 10 or any(not text(item) for item in argv) or argv[0] != posture["node_path"]:
                return None
            if not isinstance(paths, list) or len(paths) > 8 or any(not path(item) for item in paths):
                return None
            try:
                canonical, config = canonical_node_command(name, command["body"], posture["node_path"])
            except RepoSandboxError:
                return None
            if command != canonical:
                return None
            if config is not None:
                compiler_config = config
        if plan["output_directory"] is not None and not path(plan["output_directory"]):
            return None
        output = plan["output_directory"]
        if output is not None and (compiler_config is None or output in {"src", "tests", "node_modules"} or output.startswith("node_modules/")):
            return None
        if any(not digest(plan[key]) for key in ("package_sha256", "lockfile_sha256", "dependency_manifest_sha256", "snapshot_sha256")):
            return None
        configs = plan["configs"]
        config_keys = {compiler_config, "typescript_version", "typescript_entry_sha256"} if compiler_config is not None else set()
        if not isinstance(configs, dict) or set(configs) != config_keys:
            return None
        for key, value in configs.items():
            if key == "typescript_version":
                if not text(value, 128) or re.fullmatch(r"\d+\.\d+\.\d+", value) is None: return None
            elif key == "typescript_entry_sha256" or (path(key) and key.endswith(".json")):
                if not digest(value): return None
            else:
                return None
        aliases = plan["dependency_aliases"]
        if not isinstance(aliases, dict) or len(aliases) > 2:
            return None
        allowed_aliases = {"node_modules/.bin/tsc": "alias:../typescript/bin/tsc", "node_modules/.bin/tsserver": "alias:../typescript/bin/tsserver"}
        if any(key not in allowed_aliases or value != allowed_aliases[key] for key, value in aliases.items()):
            return None
    encoded = json.dumps(posture, sort_keys=True, separators=(",", ":")).encode()
    if len(encoded) > 16384 or hashlib.sha256(encoded).hexdigest() != authority.get("executor_posture_digest"):
        return None
    expected = json.dumps(expected_posture, sort_keys=True, separators=(",", ":")).encode()
    if encoded != expected:
        return None
    return json.loads(encoded)


def execution_plan(root: Path, arguments: Any, allowed: Any, identity: Mapping[str, Any]) -> dict[str, Any]:
    selection = normalize_selection(arguments)
    if any(immutable(path) for path in allowed):
        raise RepoSandboxError("Node package, lock, config and dependencies cannot be patch targets")
    package = json_regular(root, "package.json")
    lock = json_regular(root, "package-lock.json")
    if lock.get("lockfileVersion") not in {2, 3} or not isinstance(lock.get("packages"), dict):
        raise RepoSandboxError("Node requires an inspected package-lock v2/v3 snapshot")
    scripts = package.get("scripts")
    if not isinstance(scripts, dict):
        raise RepoSandboxError("Node requires named package scripts")
    commands = []
    configs = {}
    output_dir = None
    for name in SELECTIONS[selection]:
        body = scripts.get(name)
        if not isinstance(body, str) or len(body) > 4096 or body != " ".join(body.split(" ")):
            raise RepoSandboxError("Node script must have finite single-space tokens")
        tokens = body.split(" ")
        argv = [str(identity["node_path"])]
        if name == "test" and tokens[:2] == ["node", "--test"] and 1 <= len(tokens[2:]) <= 8:
            paths = [relative(path) for path in tokens[2:]]
            if any(not path.endswith((".test.js", ".test.mjs")) for path in paths):
                raise RepoSandboxError("Node --test accepts only exact test.js/test.mjs paths")
            argv += ["--test", *paths]
        elif tokens[:1] == ["node"] and len(tokens) == 2:
            paths = [relative(tokens[1])]
            if not paths[0].endswith((".js", ".mjs", ".cjs")):
                raise RepoSandboxError("Node entrypoint must be JavaScript")
            argv += paths
        elif name == "build" and len(tokens) == 3 and tokens[:2] == ["tsc", "--project"]:
            config_path = relative(tokens[2])
            if not config_path.endswith(".json"):
                raise RepoSandboxError("TypeScript config must be an inspected JSON path")
            config = json_regular(root, config_path)
            options = config.get("compilerOptions", {})
            if not isinstance(options, dict) or "extends" in config or "references" in config or "plugins" in options:
                raise RepoSandboxError("TypeScript extended/project/plugin configs are not supported")
            for value in [*config.get("include", []), *config.get("exclude", []), *config.get("files", [])]:
                if not isinstance(value, str) or value.startswith(("/", "-")) or ".." in PurePosixPath(value).parts or "\\" in value:
                    raise RepoSandboxError("TypeScript config paths must remain in the stage")
            for key in ("rootDir", "outDir", "tsBuildInfoFile", "baseUrl"):
                if key in options:
                    relative(str(options[key]))
            if any(key in options for key in ("typeRoots", "paths", "outFile")):
                raise RepoSandboxError("TypeScript external resolution/output mapping is unsupported")
            if options.get("noEmit") is not True:
                output_dir = relative(str(options.get("outDir", "")))
                if output_dir.startswith("node_modules/") or output_dir in {"src", "tests"}:
                    raise RepoSandboxError("TypeScript requires a separate finite output directory")
            compiler = "node_modules/typescript/lib/tsc.js"
            dependency = json_regular(root, "node_modules/typescript/package.json")
            locked = lock["packages"].get("node_modules/typescript")
            if not isinstance(locked, dict) or locked.get("version") != dependency.get("version"):
                raise RepoSandboxError("TypeScript installed version differs from the reviewed lockfile")
            read_regular(root, compiler, MAX_DEP_FILE_BYTES)
            argv += [compiler, "--project", config_path]
            configs[config_path] = hash_file(root / config_path)
            configs["typescript_version"] = dependency.get("version")
            configs["typescript_entry_sha256"] = hash_file(root / compiler)
            paths = []
        else:
            raise RepoSandboxError("Node script is outside the reviewed direct-command grammar")
        for path in paths:
            generated = name == "test" and output_dir is not None and path.startswith(output_dir + "/")
            if not generated and path not in allowed:
                raise RepoSandboxError("Node entrypoint must be an exact approved source path")
            if not generated:
                read_regular(root, path)
        canonical, _ = canonical_node_command(name, body, str(identity["node_path"]))
        if canonical != {"script":name, "body":body, "argv":argv, "paths":paths}:
            raise RepoSandboxError("Node planner disagrees with canonical command grammar")
        commands.append(canonical)
    return {"selection":list(selection), "commands":commands, "output_directory":output_dir,
            "package_sha256":hash_file(root/"package.json"), "lockfile_sha256":hash_file(root/"package-lock.json"),
            "configs":configs, "lifecycle_hooks_executed":False}


class NodeRepoRepairExecutor(LocalRepoRepairExecutor):
    """Selected local Node adapter; optional Node/Docker is explicitly blocked."""

    def snapshot_repository(self, repository_path: str | Path, staging_root: str | Path,
            *, preserve_source_modes: bool = False) -> RepositorySnapshot:
        source = self.validate_snapshot_root(repository_path)
        destination = Path(staging_root).absolute()
        if destination.exists() or destination.is_symlink():
            if destination.is_symlink() or not destination.is_dir():
                raise RepoSandboxError("Node snapshot destination is not a real directory")
            shutil.rmtree(destination)
        destination.mkdir(parents=True, mode=0o700)
        entries=[]; total=0; source_files=0; dep_files=0; directories=0
        for current, dirs, files in os.walk(source, followlinks=False):
            current_path=Path(current)
            rel=current_path.relative_to(source)
            dirs[:]=sorted(name for name in dirs if name != ".git")
            if len(rel.parts)>self.limits.max_depth:
                raise RepoSandboxError("Node snapshot depth exceeded")
            for name in dirs:
                path=current_path/name
                if path.is_symlink():
                    raise RepoSandboxError("Node dependency/package directory symlinks are blocked")
                directories+=1
                if directories>self.limits.max_directories:
                    raise RepoSandboxError("Node snapshot directory bound exceeded")
            for name in sorted(files):
                path=current_path/name; relative_path=path.relative_to(source).as_posix()
                dependency=relative_path.startswith("node_modules/")
                metadata=path.lstat()
                copied_name=relative_path
                alias_kind="file"
                if stat.S_ISLNK(metadata.st_mode):
                    # Only reviewed aliases inside .bin may name a snapshot-local
                    # regular dependency. Copy the bytes, never execute an alias.
                    if not relative_path.startswith("node_modules/.bin/"):
                        raise RepoSandboxError("Node symlink is outside the reviewed alias directory")
                    if name not in {"tsc", "tsserver"}:
                        raise RepoSandboxError("Node executable alias is not in the reviewed TypeScript profile")
                    alias_kind="alias:"+os.readlink(path)
                    resolved=path.resolve(strict=True)
                    try:
                        copied_name=resolved.relative_to(source).as_posix()
                    except ValueError as exc:
                        raise RepoSandboxError("Node alias escapes the dependency snapshot") from exc
                    expected_alias = "node_modules/typescript/bin/" + path.name
                    if copied_name != expected_alias:
                        raise RepoSandboxError("Node alias target is not a dependency file")
                    metadata=resolved.lstat()
                if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink!=1:
                    raise RepoSandboxError("Node snapshot requires regular unlinked files")
                bound=MAX_DEP_FILE_BYTES if dependency else self.limits.max_file_bytes
                if preserve_source_modes:
                    data, opened_metadata = _read_regular_with_metadata(source, copied_name, bound)
                else:
                    data=read_regular(source,copied_name,bound)
                total+=len(data)
                dep_files+=int(dependency);source_files+=int(not dependency)
                if total>self.limits.max_snapshot_bytes or dep_files>MAX_DEP_FILES or source_files>self.limits.max_files:
                    raise RepoSandboxError("Node source/dependency aggregate snapshot limit exceeded")
                target=destination/relative_path;target.parent.mkdir(parents=True,exist_ok=True)
                if preserve_source_modes:
                    with target.open("xb") as output:
                        os.fchmod(output.fileno(), 0o700 if not dependency and opened_metadata.st_mode & 0o111 else 0o600)
                        output.write(data)
                else:
                    target.write_bytes(data);target.chmod(0o600)
                entries.append(SnapshotEntry(relative_path,len(data),hashlib.sha256(data).hexdigest(),alias_kind))
        return RepositorySnapshot(str(source),str(destination),_digest_entries(entries),tuple(entries),total)

    def preflight(self, authority: Mapping[str, Any] | None=None, *, deadline_at: float | None=None) -> RepoSandboxPreflight:
        posture={"kind":str(self.config.executor_kind),"profile":PROFILE,"isolation_claim":"none","network_isolation":"not_verified",
                 "resource_enforcement":"admission_and_wall_timeout_only","host_access":"explicit_job_approval_required",
                 "limits_digest":limits_digest(self.limits),"process_supervision":"linux_per_job_subreaper",
                 "unenforced_limits":["cpu","memory","pids"],"dependency_limits":{"files":MAX_DEP_FILES,"file_bytes":MAX_DEP_FILE_BYTES,"aggregate_bytes":self.limits.max_snapshot_bytes}}
        try:
            if self.config.executor_kind!="local":
                posture.update(isolation_claim="unverified",network_isolation="unverified",resource_enforcement="unverified")
                posture.pop("host_access")
                raise RepoSandboxError("node_docker_profile_unverified")
            if not self.config.enabled:
                raise RepoSandboxError("repo_sandbox_disabled")
            self._trusted_workspace()
            identity=runtime_identity(self.config)
            posture.update(identity)
            if authority and authority.get("repository_ref"):
                root=self.validate_snapshot_root(str(authority["repository_ref"]))
                # Approval binds original dependency link topology as well as
                # the copied bytes through an inspected private snapshot.
                import tempfile
                with tempfile.TemporaryDirectory(prefix="node-preflight-",dir=self._trusted_staging_directory()) as directory:
                    snapshot=self.snapshot_repository(root,Path(directory)/"snapshot")
                    plan=execution_plan(Path(snapshot.staging_root),authority.get("test_args",()),authority.get("allowed_paths",()),identity)
                    dependencies=[{"relative_path":entry.relative_path,"size_bytes":entry.size_bytes,"sha256":entry.sha256} for entry in snapshot.entries if entry.relative_path.startswith("node_modules/")]
                    plan["dependency_manifest_sha256"]=executor_posture_digest({"entries":dependencies})
                    plan["dependency_aliases"]={entry.relative_path:entry.kind for entry in snapshot.entries if entry.kind.startswith("alias:")}
                    plan["snapshot_sha256"]=snapshot.digest
                    posture["execution_plan"]=plan
            return RepoSandboxPreflight(True,"ready","node_local_staging_available",info={"runtime_identity":identity},executor_kind="local",posture=posture,posture_digest=executor_posture_digest(posture))
        except (OSError, TypeError, ValueError, subprocess.SubprocessError, RepoSandboxError) as exc:
            return RepoSandboxPreflight(False,"blocked",str(exc)[:512],executor_kind=str(self.config.executor_kind),posture=posture)

    def execute_job(self, job: RepoSandboxJob, *, before_dispatch: Callable[[],None] | None=None) -> dict[str,Any]:
        from src.execution.repo_supervisor import exact_signal, start_identity, finish_supervisor
        from src.execution.repo_sandbox import iteration_process_projection
        if job.iteration_binding is not None:
            from src.workflows.repo_repair_source import assert_repo_iteration_process_binding
            assert_repo_iteration_process_binding(job.iteration_binding, job)
        if not 1<=job.deadline_seconds<=self.limits.max_wall_seconds:
            raise RepoSandboxError("Node wall deadline exceeds the selected profile")
        deadline=time.monotonic()+job.deadline_seconds
        if job.execution_deadline_at:
            remaining=datetime.fromisoformat(job.execution_deadline_at.replace("Z","+00:00")).timestamp()-time.time()
            deadline=min(deadline,time.monotonic()+max(0,remaining))
        preflight=self.preflight({"repository_ref":job.repository_root,"test_args":job.test_args,"allowed_paths":job.allowed_paths})
        if not preflight.ok:
            return {"status":"blocked","reason":preflight.reason,"preflight":preflight.as_receipt(),"learning":"no_learning"}
        if job.expected_posture_digest and job.expected_posture_digest!=preflight.posture_digest:
            raise RepoSandboxError("Node execution inputs changed after approval")
        plan=preflight.posture["execution_plan"]
        if plan["snapshot_sha256"]!=job.base_digest:
            raise RepoSandboxError("Node source snapshot changed after inspection")
        patch_paths=_patch_paths_from_diff(job.patch_bytes,job.allowed_paths)
        if any(immutable(path) for path in patch_paths):
            raise RepoSandboxError("Node immutable execution inputs cannot be patched")
        if time.monotonic()>=deadline:
            raise RepoSandboxError("Node deadline exhausted before dispatch")
        token=self._job_stage_token(job)
        existing = self._read_job_marker(job.job_id)
        if existing is not None and job.iteration_binding is not None:
            prior = existing.get("iteration_binding") or {}
            if (existing.get("phase") != "iteration_cleanup_verified" or existing.get("status") != "iteration_failed_quiescent"
                or existing.get("cleanup_proven") is not True or prior.get("repository_job_id") != job.job_id
                or prior.get("repository_attempt_id") != job.attempt_id or prior.get("repository_fence") != job.fencing_token
                or prior.get("iteration_index") != job.iteration_binding.iteration_index - 1
                or prior.get("authority_digest") != existing.get("authority_digest")):
                raise RepoSandboxError("Original Node iteration cleanup is unproven", terminal_status="unknown_external_effect")
            existing = None
        if existing is not None:
            raise RepoSandboxError("Node job already owns a marker; recovery required",terminal_status="unknown_external_effect")
        marker={"schema":"seraph.repo_repair_local_job.v1","job_id":job.job_id,"authority_digest":job.authority_digest,"profile":PROFILE,
                "attempt_id":job.attempt_id or "legacy-attempt","fencing_token":job.fencing_token,"base_digest":job.base_digest,"posture_digest":preflight.posture_digest,
                "stage_binding":{"executor_kind":"local","job_id":job.job_id,"attempt_id":job.attempt_id or "legacy-attempt","fencing_token":job.fencing_token,"authority_digest":job.authority_digest},
                "supervisor_token":token,"phase":"admitted","status":"running","cleanup_proven":False}
        if job.iteration_binding is not None:
            marker["iteration_binding"] = iteration_process_projection(job.iteration_binding)
            marker["stage_binding"]["iteration_id"] = job.iteration_binding.iteration_id
        self._write_job_marker(job.job_id,marker)
        with self._active_lock:
            self._active[job.job_id]={"process":None,"cancelled":False}
        process=None
        transport_complete=False
        try:
            with self._job_staging_directory(token) as stage:
                marker.update(stage_directory=str(stage.relative_to(self.workspace_dir)),stage_identity={"device":stage.stat().st_dev,"inode":stage.stat().st_ino})
                snapshot=self.snapshot_repository(job.repository_root,stage/"workspace")
                if snapshot.digest!=job.base_digest:
                    raise RepoSandboxError("Node source drift before dispatch")
                (stage/"patch.diff").write_bytes(job.patch_bytes)
                (stage/"out").mkdir(mode=0o700)
                env=self._minimal_env(stage);env.pop("PYTHONPATH",None);env["PATH"]="/usr/bin:/bin"
                env["npm_config_cache"]=str(stage/"home"/"cache")
                payload={"profile":PROFILE,"stage":str(stage),"plan":plan,"runtime":preflight.info["runtime_identity"],"deadline_at":deadline,
                         "allowed_paths":list(job.allowed_paths),"patch_paths":patch_paths,"job_id":job.job_id,"authority_digest":job.authority_digest,
                         "token":token,"environment":env,"limits":asdict(self.limits)}
                if job.iteration_binding is not None:
                    payload["iteration_binding"] = iteration_process_projection(job.iteration_binding)
                request=stage/"supervisor.json";request.write_text(json.dumps(payload));request.chmod(0o600)
                marker["phase"]="dispatch_fence_pending";self._write_job_marker(job.job_id,marker)
                if before_dispatch:before_dispatch()
                if time.monotonic()>=deadline:
                    raise RepoSandboxError("Node deadline exhausted before supervisor launch")
                supervisor=Path(__file__).with_name("repo_supervisor.py")
                process=subprocess.Popen([sys.executable,"-I",str(supervisor),str(request)],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,env=env,start_new_session=True)
                pid_start=start_identity(process.pid)
                if pid_start is None:
                    raise RepoSandboxError("Node supervisor identity unavailable",terminal_status="unknown_external_effect")
                marker.update(phase="worker_started",pid=process.pid,pid_start_identity=pid_start,supervisor_source_sha256=preflight.posture["supervisor_source_sha256"])
                with self._active_lock:
                    self._active[job.job_id].update(process=process,pid_start_identity=pid_start)
                with self._job_marker_lock(job.job_id, timeout_seconds=max(0, deadline-time.monotonic())):
                    self._write_job_marker_locked(job.job_id,marker)
                    # The committed cancellation and exact execution binding
                    # are checked in the same critical section as token write.
                    current=self._read_job_marker(job.job_id)
                    expected={"job_id":job.job_id,"authority_digest":job.authority_digest,
                              "attempt_id":job.attempt_id or "legacy-attempt","fencing_token":job.fencing_token,
                              "supervisor_token":token,"pid":process.pid,"pid_start_identity":pid_start,
                              "profile":PROFILE,"base_digest":job.base_digest,"posture_digest":preflight.posture_digest,
                              "stage_binding":marker["stage_binding"],"supervisor_source_sha256":preflight.posture["supervisor_source_sha256"]}
                    if current is None or any(current.get(key)!=value for key,value in expected.items()) or current.get("status") not in {"running","cancellation_requested"} or time.monotonic()>=deadline:
                        exact_signal(process.pid,pid_start,signal.SIGTERM)
                        raise RepoSandboxError("Node dispatch binding changed or deadline expired",terminal_status="unknown_external_effect")
                    if current.get("cancellation_requested") is True or current.get("status")=="cancellation_requested":
                        process.stdin.write(("cancel:"+token+"\n").encode());process.stdin.flush()
                    else:
                        process.stdin.write((token+"\n").encode());process.stdin.flush()
                process.stdin.close()
                transport = finish_supervisor(process, deadline=deadline, stream_limit=self.limits.max_stream_bytes)
                transport_complete=True
                result_path=stage/"out"/"supervisor-result.json"
                result=json.loads(self._read_private_output(stage/"out","supervisor-result.json"))
                if process.returncode!=0 or result.get("job_id")!=job.job_id or result.get("profile")!=PROFILE or result.get("token")!=token or result.get("supervisor_pid")!=process.pid or result.get("supervisor_start")!=pid_start or result.get("cleanup_proven") is not True:
                    raise RepoSandboxError("Node supervisor terminal cleanup is unproven",terminal_status="unknown_external_effect")
                if job.iteration_binding is not None and result.get("iteration_binding") != iteration_process_projection(job.iteration_binding):
                    raise RepoSandboxError("Original Node iteration readback changed", terminal_status="unknown_external_effect")
                if job.iteration_binding is not None:
                    tested = result.get("tested_file_hash_metadata")
                    if (not isinstance(result.get("after_digest"), str)
                            or re.fullmatch(r"[0-9a-f]{64}", result["after_digest"]) is None
                            or not isinstance(tested, list) or not tested
                            or len(tested) > self.limits.max_files + MAX_DEP_FILES
                            or any(not isinstance(entry, dict) or set(entry) != {"path", "size_bytes", "sha256"}
                                or not isinstance(entry["path"], str) or entry["path"].startswith("/")
                                or ".." in entry["path"].split("/")
                                or type(entry["size_bytes"]) is not int or not 0 <= entry["size_bytes"] <= (
                                    MAX_DEP_FILE_BYTES if entry["path"].startswith("node_modules/") else self.limits.max_file_bytes)
                                or not isinstance(entry["sha256"], str) or re.fullmatch(r"[0-9a-f]{64}", entry["sha256"]) is None
                                for entry in tested)
                            or len({entry["path"] for entry in tested}) != len(tested)):
                        raise RepoSandboxError("Actual Node tested snapshot readback missing or changed", terminal_status="unknown_external_effect")
                    if _digest_entries(SnapshotEntry(entry["path"], entry["size_bytes"], entry["sha256"])
                            for entry in tested) != result["after_digest"]:
                        raise RepoSandboxError("Actual Node tested tree and file hashes disagree", terminal_status="unknown_external_effect")
                outputs={name:self._read_private_output(stage/"out",name) for name in ("diff.patch","pytest.stdout","pytest.stderr","build.stdout","build.stderr")}
                if hashlib.sha256(outputs["diff.patch"]).hexdigest()!=result.get("diff_sha256"):
                    raise RepoSandboxError("Node exported diff readback hash changed",terminal_status="unknown_external_effect")
                for command in result.get("commands",[]):
                    name="pytest" if command.get("script")=="test" else "build"
                    if hashlib.sha256(outputs[name+".stdout"]).hexdigest()!=command.get("stdout_sha256") or hashlib.sha256(outputs[name+".stderr"]).hexdigest()!=command.get("stderr_sha256"):
                        raise RepoSandboxError("Node test/build output readback hash changed",terminal_status="unknown_external_effect")
                self._assert_stage_identity(stage,marker["stage_identity"])
                original=self.snapshot_repository(job.repository_root,stage/"original-after")
                if original.digest!=job.base_digest:
                    raise RepoSandboxError("Node original source/dependency drift after execution",terminal_status="unknown_external_effect")
                current=self._read_job_marker(job.job_id)
                status="cancelled" if current and current.get("status")=="cancellation_requested" else result["status"]
                manifest={**result,"schema":"seraph.repo_repair_execution.v1","status":status,"profile":PROFILE,"executor_kind":"local",
                          "authority_digest":job.authority_digest,"posture_digest":preflight.posture_digest,"execution_identity":preflight.info["runtime_identity"],
                          "execution_plan":plan,"source_original_unchanged":True,"isolation_claim":"none","network_isolation":"not_verified",
                          "resource_enforcement":"admission_and_wall_timeout_only","learning":"no_learning",
                          "supervisor_transport": transport,
                          "supervisor_identity": {"pid": process.pid, "start_identity": pid_start,
                              "source_sha256": preflight.posture["supervisor_source_sha256"], "token": token},
                          **({"iteration_binding": iteration_process_projection(job.iteration_binding), "stage_removed": True} if job.iteration_binding is not None else {})}
                encoded=json.dumps(manifest,sort_keys=True).encode()+b"\n"
                if time.monotonic()>=deadline:
                    raise RepoSandboxError("Node cleanup/readback deadline exhausted",terminal_status="unknown_external_effect")
                shutil.rmtree(stage)
                if stage.exists():
                    raise RepoSandboxError("Original Node stage cleanup unproven", terminal_status="unknown_external_effect")
                marker.update(phase="iteration_cleanup_verified" if job.iteration_binding is not None else "cleanup_verified",
                              status="iteration_failed_quiescent" if job.iteration_binding is not None and status == "failed" else status,cleanup_proven=True,process_cleanup=result,
                              terminal_receipt={"status":status,"manifest_sha256":hashlib.sha256(encoded).hexdigest(),"readback_sha256":hashlib.sha256(encoded).hexdigest(),"stage_binding":marker["stage_binding"]})
                self._write_job_marker(job.job_id,marker)
                terminal = {"status":status,"failure_reason":result.get("reason"),"manifest":manifest,"readback":manifest,
                        "outputs":{**outputs,"manifest.json":encoded,"readback.json":encoded},"effective_profile":preflight.posture,
                        "cleanup":{"status":"cleanup_verified","cleanup_proven":True},"checkpoint_phases":["admitted","input_loaded","worker_started","tests_finished","output_exported","readback_verified","cleanup_verified"],"learning":"no_learning","operator_visible":True}
                if job.iteration_binding is not None:
                    self._owned_iteration_terminal[(job.job_id, job.iteration_binding.iteration_id)] = terminal
                    terminal["iteration_cleanup_witness"] = self._iteration_cleanup_witness(job, terminal)
                    terminal["cleanup"] = {"status": "iteration_cleanup_verified", "cleanup_proven": True}
                return terminal
        except (OSError, ValueError, subprocess.SubprocessError, RepoSandboxError) as exc:
            current=self._read_job_marker(job.job_id) or marker
            self._write_job_marker(job.job_id,{**current,"status":"unknown_external_effect","cleanup_proven":False,"reason":str(exc)[:512]})
            # Ask the exact supervisor to clean; never kill it and fabricate
            # descendant cleanup. Missing/dead supervisor retains the slot.
            if process is not None and process.poll() is None:
                exact_signal(process.pid,str(marker.get("pid_start_identity") or ""),signal.SIGTERM)
            raise RepoSandboxError(str(exc),phase="cleanup",terminal_status="unknown_external_effect") from exc
        finally:
            current = self._read_job_marker(job.job_id) if job.iteration_binding is not None else None
            if job.iteration_binding is None or (current and current.get("phase") == "iteration_cleanup_verified" and current.get("cleanup_proven") is True):
                with self._active_lock:self._active.pop(job.job_id,None)
            if process is not None and (transport_complete or job.iteration_binding is None):
                for stream in (process.stdin,process.stdout,process.stderr):
                    if stream and not stream.closed:stream.close()

    def _read_cancel_marker_locked(self, job_id: str) -> tuple[dict[str,Any], os.stat_result]:
        """Read the exact private marker before a cancellation decision."""
        directory=_open_trusted_directory(self._job_marker_directory)
        descriptor=-1
        try:
            name=self._job_marker_name(job_id)
            descriptor=os.open(name,os.O_RDONLY|os.O_CLOEXEC|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=directory)
            opened=os.fstat(descriptor)
            if (not stat.S_ISREG(opened.st_mode) or opened.st_uid!=os.getuid()
                or stat.S_IMODE(opened.st_mode)!=0o600 or opened.st_nlink!=1 or opened.st_size>16*1024):
                raise RepoSandboxError("Node cancellation marker is not private")
            raw=os.read(descriptor,16*1024+1)
            if len(raw)>16*1024:
                raise RepoSandboxError("Node cancellation marker exceeds its bound")
            marker=json.loads(raw)
            named=os.stat(name,dir_fd=directory,follow_symlinks=False)
            if (not isinstance(marker,dict) or not _same_file_metadata(opened,os.fstat(descriptor))
                or not _same_file_metadata(opened,named)):
                raise RepoSandboxError("Node cancellation marker identity changed")
            return marker,opened
        finally:
            if descriptor>=0:os.close(descriptor)
            os.close(directory)

    def cancel(self, *, job_id: str | None=None, authority: Mapping[str,Any] | None=None, **kwargs:Any) -> dict[str,Any]:
        from src.execution.repo_supervisor import exact_signal
        supplied=authority or {};resolved=job_id or str(supplied.get("job_id") or "")
        with self._job_marker_lock(resolved):
            try:
                marker,marker_metadata=self._read_cancel_marker_locked(resolved)
            except (OSError,ValueError,RepoSandboxError):
                return {"status":"unknown_external_effect","reason":"node_supervisor_identity_missing","cleanup_proven":False}
            if marker is None or marker.get("profile")!=PROFILE:
                return {"status":"unknown_external_effect","reason":"node_supervisor_identity_missing","cleanup_proven":False}
            if not isinstance(marker.get("status"),str):
                return {"status":"unknown_external_effect","reason":"node_supervisor_identity_missing","cleanup_proven":False}
            for key in ("authority_digest","attempt_id","fencing_token"):
                if key in supplied and supplied[key]!=marker.get(key):
                    return {"status":"unknown_external_effect","reason":"node_supervisor_authority_changed","cleanup_proven":False}
            if marker.get("attempt_id")!="legacy-attempt" and "attempt_id" not in supplied:
                return {"status":"unknown_external_effect","reason":"node_supervisor_attempt_missing","cleanup_proven":False}
            if marker.get("fencing_token",0)!=0 and "fencing_token" not in supplied:
                return {"status":"unknown_external_effect","reason":"node_supervisor_fence_missing","cleanup_proven":False}
            if "fencing_token" in supplied and type(supplied["fencing_token"]) is not int:
                return {"status":"unknown_external_effect","reason":"node_supervisor_fence_invalid","cleanup_proven":False}
            if (marker.get("phase")=="cleanup_verified" or marker.get("cleanup_proven") is True
                or marker.get("status") in {"cancelled","succeeded","failed","unknown_external_effect"}):
                # A late API cancel can arrive after the supervisor's original
                # ECHILD receipt was committed. Do not erase it or signal the
                # reaped supervisor; ordinary recovery owns capacity release.
                try:
                    binding={"executor_kind":"local","job_id":resolved,"authority_digest":marker.get("authority_digest"),
                             "attempt_id":marker.get("attempt_id"),"fencing_token":marker.get("fencing_token")}
                    terminal=marker.get("terminal_receipt")
                    stage_identity=marker.get("stage_identity")
                    if (marker.get("schema")!="seraph.repo_repair_local_job.v1" or marker.get("job_id")!=resolved
                        or "executor_kind" in marker and marker["executor_kind"]!="local"
                        or not isinstance(binding["authority_digest"],str) or not binding["authority_digest"]
                        or not isinstance(binding["attempt_id"],str) or not binding["attempt_id"]
                        or type(binding["fencing_token"]) is not int or binding["fencing_token"]<0
                        or any(not isinstance(marker.get(key),str) or not re.fullmatch(r"[0-9a-f]{64}",marker[key])
                               for key in ("base_digest","posture_digest"))
                        or marker.get("stage_binding")!=binding or type(marker["stage_binding"].get("fencing_token")) is not int
                        or not isinstance(stage_identity,dict) or set(stage_identity)!={"device","inode"}
                        or any(type(value) is not int or value<0 for value in stage_identity.values())
                        or not isinstance(terminal,dict) or terminal.get("status")!="cancelled"
                        or terminal.get("stage_binding")!=binding or type(terminal["stage_binding"].get("fencing_token")) is not int
                        or not isinstance(terminal.get("manifest_sha256"),str)
                        or not re.fullmatch(r"[0-9a-f]{64}",terminal["manifest_sha256"])
                        or terminal.get("manifest_sha256")!=terminal.get("readback_sha256")
                        or any(key in supplied and supplied[key]!=marker.get(key) for key in ("job_id","base_digest","posture_digest"))):
                        raise RepoSandboxError("Node terminal cancellation binding is unproven")
                    posture={"supervisor_source_sha256":hash_file(Path(__file__).with_name("repo_supervisor.py"))}
                    proof_authority={"base_digest":marker.get("base_digest"),"executor_posture_digest":marker.get("posture_digest"),
                                     "executor_posture":posture}
                    if ("executor_posture_digest" in supplied and supplied["executor_posture_digest"]!=marker.get("posture_digest")
                        or "executor_posture" in supplied and (not isinstance(supplied["executor_posture"],dict)
                            or supplied["executor_posture"].get("supervisor_source_sha256")!=posture["supervisor_source_sha256"])):
                        raise RepoSandboxError("Node terminal cancellation posture changed")
                    readback=self._process_cleanup_readback_locked(job_id=resolved,attempt_id=binding["attempt_id"],
                        authority_digest=binding["authority_digest"],fencing_token=binding["fencing_token"],authority=proof_authority)
                    if readback!=hashlib.sha256(json.dumps(marker,sort_keys=True,separators=(",",":")).encode()).hexdigest():
                        raise RepoSandboxError("Node terminal cancellation marker changed")
                    token=self._stage_binding_token(binding)
                    staging=self.workspace_dir/"artifacts"/"repo-sandbox"/"staging"
                    if marker.get("stage_directory")!=str((staging/token).relative_to(self.workspace_dir)):
                        raise RepoSandboxError("Node terminal cancellation stage changed")
                    directory=_open_trusted_directory(staging)
                    try:
                        parent=os.fstat(directory)
                        if parent.st_uid!=os.getuid() or stat.S_IMODE(parent.st_mode)!=0o700:
                            raise RepoSandboxError("Node terminal cancellation staging parent is untrusted")
                        try:os.stat(token,dir_fd=directory,follow_symlinks=False)
                        except FileNotFoundError:pass
                        else:raise RepoSandboxError("Node terminal cancellation stage cleanup is unproven")
                    finally:os.close(directory)
                    checked,checked_metadata=self._read_cancel_marker_locked(resolved)
                    if checked!=marker or not _same_file_metadata(marker_metadata,checked_metadata):
                        raise RepoSandboxError("Node terminal cancellation identity changed")
                except (OSError,ValueError,RepoSandboxError):
                    return {"status":"unknown_external_effect","reason":"node_terminal_cancellation_unproven","cleanup_proven":False}
                return {"status":"cancel_requested","cleanup_proven":False,"job_id":resolved}
            self._write_job_marker_locked(resolved,{**marker,"status":"cancellation_requested","cancellation_requested":True,"phase":"cancel_requested"})
            pid=marker.get("pid");start=marker.get("pid_start_identity")
            try:
                signalled=isinstance(pid,int) and isinstance(start,str) and exact_signal(pid,start,signal.SIGTERM)
            except (OSError,ValueError):
                signalled=False
            if not signalled:
                return {"status":"unknown_external_effect","reason":"node_supervisor_identity_missing_or_changed","cleanup_proven":False}
            return {"status":"cancel_requested","cleanup_proven":False,"job_id":resolved}

    def _process_cleanup_readback_locked(self, *, job_id: str, attempt_id: str, authority_digest: str,
                                         fencing_token: int, authority: Mapping[str, Any]) -> str:
        """Read physical-only proof under the caller-held private marker lock."""
        marker=self._read_job_marker(job_id)
        binding={"executor_kind":"local","job_id":job_id,"attempt_id":attempt_id,
                 "fencing_token":fencing_token,"authority_digest":authority_digest}
        token=hashlib.sha256(json.dumps(binding,sort_keys=True,separators=(",",":")).encode()).hexdigest()
        posture=authority.get("executor_posture")
        expected_hash=posture.get("supervisor_source_sha256") if isinstance(posture,dict) else None
        expected={"job_id":job_id,"attempt_id":attempt_id,"authority_digest":authority_digest,
                  "fencing_token":fencing_token,"profile":PROFILE,"supervisor_token":token,
                  "stage_binding":binding,"base_digest":authority.get("base_digest"),
                  "posture_digest":authority.get("executor_posture_digest"),"supervisor_source_sha256":expected_hash,
                  "cleanup_proven":True,"status":"cancelled","phase":"cleanup_verified"}
        if marker is None or not isinstance(expected_hash,str) or not re.fullmatch(r"[0-9a-f]{64}",expected_hash) or any(marker.get(key)!=value for key,value in expected.items()):
            raise RepoSandboxError("Node original cancelled cleanup binding is unproven")
        result=marker.get("process_cleanup")
        if not isinstance(result,dict):
            raise RepoSandboxError("Node original cleanup result is missing")
        proof=result.get("process_cleanup")
        if (type(marker.get("pid")) is not int or marker["pid"]<=0 or not isinstance(marker.get("pid_start_identity"),str) or not marker["pid_start_identity"]
            or any(result.get(key)!=value for key,value in {"profile":PROFILE,"job_id":job_id,"token":token,
                    "supervisor_pid":marker["pid"],"supervisor_start":marker["pid_start_identity"],"status":"cancelled","cleanup_proven":True}.items())
            or not isinstance(proof,dict) or proof.get("cleanup_proven") is not True or proof.get("oracle")!="linux_subreaper_waitpid_echild"):
            raise RepoSandboxError("Node exact supervisor ECHILD readback is unproven")
        return hashlib.sha256(json.dumps(marker,sort_keys=True,separators=(",",":")).encode()).hexdigest()

    def reconcile(self, authority: Mapping[str,Any] | None=None) -> dict[str,Any]:
        # A gone supervisor is not proof of empty ancestry. Only the owning
        # terminal receipt can close cleanup; existing durable adopter checks
        # keep missing output/result state unknown and retain the physical slot.
        supplied=authority or {}
        marker=self._read_job_marker(str(supplied.get("job_id") or ""))
        if marker is not None and marker.get("cleanup_proven") is True:
            result=marker.get("process_cleanup")
            proof=result.get("process_cleanup") if isinstance(result,dict) else None
            valid=(marker.get("profile")==PROFILE and isinstance(result,dict) and isinstance(proof,dict)
                   and result.get("profile")==PROFILE and result.get("job_id")==supplied.get("job_id")
                   and result.get("token")==marker.get("supervisor_token")
                   and result.get("supervisor_pid")==marker.get("pid")
                   and result.get("supervisor_start")==marker.get("pid_start_identity")
                   and isinstance(marker.get("pid"),int) and isinstance(marker.get("pid_start_identity"),str)
                   and result.get("cleanup_proven") is True and proof.get("cleanup_proven") is True
                   and proof.get("oracle")=="linux_subreaper_waitpid_echild")
            if not valid:
                return {"status":"unknown_external_effect","reason":"node_exact_supervisor_cleanup_proof_missing","cleanup_proven":False,"learning":"no_learning"}
        return super().reconcile(authority)
