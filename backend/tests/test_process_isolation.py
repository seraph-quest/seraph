"""OS-enforced workspace view for agent process tools (#1103).

Real bubblewrap, no network, no provider calls.  Every channel test bakes the
target path into script content, so the argument-level checks of #1102 never
see it: only the kernel boundary can refuse it.  Hosts without the sandbox
skip, except where ``SERAPH_REQUIRE_REPO_ISOLATION_TESTS=1`` makes that a
failure (CI sets it once bwrap works on the runner).
"""
from __future__ import annotations

import errno
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import textwrap
import time
from types import SimpleNamespace
import zipfile
import base64

import pytest

from config.settings import settings
from src.execution import process_isolation
from src.extensions import capability_execution
from src.tools import process_tools as process_tools_module
from src.tools.process_tools import process_runtime_manager, run_command, start_process
from src.workspace import canonical_workspace_registry
from tests.repo_isolation_support import isolation_unavailable

SEED = {
    ".vault-key": "vault-secret\n",
    "seraph.db": "database-bytes\n",
    "google_calendar_token.json": '{"token": "secret"}\n',
    ".seraph-workspace-maintenance.lock": "",
    "extensions-state.json": '{"state": 1}\n',
    "extensions/pkg/manifest.json": '{"manifest": 1}\n',
    "artifacts/mail/private/inbox.eml": "private mail\n",
    "soul.md": "soul\n",
    "notes/a.txt": "alpha\n",
    "work/input.txt": "input\n",
    # Top-level directories exist up front: commands cannot create new
    # entries at the read-only workspace top level.
    "projects/README.md": "projects\n",
}
DENIED_FILES = (".vault-key", "seraph.db", "google_calendar_token.json", ".seraph-workspace-maintenance.lock")
READ_ONLY_FILES = ("extensions-state.json", "extensions/pkg/manifest.json")
ABSENT_PROTECTED = ("mcp-servers.json", "stdio-proxies.json", "seraph.db-wal")


def _snapshot(root: Path) -> dict[str, str]:
    """Every host entry (no symlink following) with a content digest."""
    entries = {}
    for current, directories, files in os.walk(root):
        for name in directories + files:
            path = Path(current) / name
            relative = path.relative_to(root).as_posix()
            if relative == "work" or relative.startswith("work/"):
                continue  # the tests' own probe scripts
            if path.is_symlink():
                entries[relative] = "link:" + os.readlink(path)
            elif path.is_file():
                entries[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
            else:
                entries[relative] = "dir"
    return entries


@pytest.fixture
def workspace(tmp_path_factory, monkeypatch):
    root = tmp_path_factory.mktemp("sandbox-workspace").resolve()
    monkeypatch.setattr(settings, "workspace_dir", str(root))
    process_runtime_manager.reset_for_tests()
    posture = process_tools_module._sandbox_posture()
    if not posture.isolated:
        isolation_unavailable(posture.reason)
    for relative, content in SEED.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    yield root
    process_runtime_manager.reset_for_tests()


def _script(root: Path, name: str, body: str) -> str:
    path = root / "work" / name
    path.write_text(textwrap.dedent(body))
    return f"work/{name}"


def _python(root: Path, body: str, *, cwd: str = "", timeout: int = 30) -> dict:
    relative = _script(root, f"probe_{hashlib.sha256(body.encode()).hexdigest()[:12]}.py", body)
    argument = os.path.relpath(root / relative, root / cwd)
    return process_runtime_manager.run_command(command="python3", args_json=json.dumps([argument]), cwd=cwd,
                                               timeout_seconds=timeout)


_CHANNELS = """
import json, os, shlex, subprocess
target = {target!r}
out = {{}}
def attempt(name, action):
    try:
        result = action()
        if isinstance(result, subprocess.CompletedProcess) and result.returncode != 0:
            out[name] = "refused"
        else:
            out[name] = "ok"
    except OSError as exc:
        out[name] = "refused:" + errno_name(exc.errno)
def errno_name(code):
    import errno
    return errno.errorcode.get(code, str(code))
attempt("read", lambda: open(target, "rb").read())
attempt("append", lambda: open(target, "ab").close())
attempt("truncate", lambda: open(target, "r+b").truncate(0))
attempt("sed_w", lambda: subprocess.run(["sed", "-n", "w " + target, "work/input.txt"], capture_output=True))
attempt("sed_i", lambda: subprocess.run(["sed", "-i", "s/e/E/", target], capture_output=True))
attempt("shell_redirect", lambda: subprocess.run("echo pwned > " + shlex.quote(target), shell=True, capture_output=True))
attempt("shell_append", lambda: subprocess.run("echo pwned >> " + shlex.quote(target), shell=True, capture_output=True))
attempt("rename_over", lambda: os.replace("work/input.txt", target))
attempt("unlink", lambda: os.unlink(target))
print(json.dumps(out))
"""


def _channels(root: Path, target: str) -> dict:
    result = _python(root, _CHANNELS.format(target=target))
    assert result["exit_code"] == 0, result
    return json.loads(result["stdout"])


@pytest.mark.parametrize("target", DENIED_FILES)
def test_denied_state_is_unreachable_through_every_script_channel(workspace, target):
    before = _snapshot(workspace)
    outcome = _channels(workspace, target)
    assert outcome["read"] == "refused:EACCES"
    assert all(value.startswith("refused") for value in outcome.values()), outcome
    assert _snapshot(workspace) == before


@pytest.mark.parametrize("target", READ_ONLY_FILES)
def test_read_only_state_is_readable_but_never_writable(workspace, target):
    before = _snapshot(workspace)
    outcome = _channels(workspace, target)
    assert outcome.pop("read") == "ok"
    assert outcome["append"] == "refused:EROFS"
    assert all(value.startswith("refused") for value in outcome.values()), outcome
    assert _snapshot(workspace) == before


def test_denied_and_read_only_directories(workspace):
    before = _snapshot(workspace)
    result = _python(workspace, """
        import json, os
        out = {}
        for name, action in {
            "private_list": lambda: os.listdir("artifacts/mail/private"),
            "private_read": lambda: open("artifacts/mail/private/inbox.eml").read(),
            "private_create": lambda: open("artifacts/mail/private/new.eml", "w").close(),
            "extensions_list": lambda: os.listdir("extensions/pkg"),
            "extensions_read": lambda: open("extensions/pkg/manifest.json").read(),
            "extensions_create": lambda: open("extensions/pkg/new.json", "w").close(),
            "extensions_mkdir": lambda: os.mkdir("extensions/new"),
            "mail_level_create": lambda: open("artifacts/mail/new.eml", "w").close(),
            "artifacts_level_create": lambda: open("artifacts/new.txt", "w").close(),
        }.items():
            try:
                action()
                out[name] = "ok"
            except OSError as exc:
                out[name] = exc.errno
        print(json.dumps(out))
    """)
    outcome = json.loads(result["stdout"])
    assert outcome["private_list"] == outcome["private_read"] == outcome["private_create"] == errno.EACCES
    assert outcome["extensions_list"] == "ok" and outcome["extensions_read"] == "ok"
    assert outcome["extensions_create"] == outcome["extensions_mkdir"] == errno.EROFS
    assert outcome["mail_level_create"] == outcome["artifacts_level_create"] == errno.EROFS
    assert _snapshot(workspace) == before


def test_absent_protected_names_cannot_be_created_and_the_host_stays_clean(workspace):
    before = _snapshot(workspace)
    result = _python(workspace, f"""
        import json, os, subprocess
        out = {{}}
        for name in {list(ABSENT_PROTECTED)!r} + [".Vault-Key", "SERAPH.DB", "seraph.db.bak", "new-top-level.txt"]:
            try:
                os.close(os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
                out[name] = "created"
            except OSError as exc:
                out[name] = exc.errno
        out["shell"] = subprocess.run("echo x > mcp-servers.json", shell=True).returncode
        print(json.dumps(out))
    """)
    outcome = json.loads(result["stdout"])
    assert "created" not in outcome.values(), outcome
    assert outcome["shell"] != 0
    assert _snapshot(workspace) == before
    assert not any((workspace / name).exists() for name in ABSENT_PROTECTED)


def test_node_scripts_are_held_to_the_same_view(workspace):
    if "node" not in process_tools_module._sandbox_runtime()[1]:
        pytest.skip("node is not installed on this host")
    before = _snapshot(workspace)
    (workspace / "work" / "probe.js").write_text(textwrap.dedent("""
        const fs = require('fs');
        const out = {};
        for (const [name, action] of Object.entries({
          vault_read: () => fs.readFileSync('.vault-key'),
          vault_write: () => fs.writeFileSync('.vault-key', 'x'),
          state_read: () => fs.readFileSync('extensions-state.json'),
          state_write: () => fs.writeFileSync('extensions-state.json', 'x'),
          notes_write: () => fs.writeFileSync('notes/node.txt', 'node'),
        })) {
          try { action(); out[name] = 'ok'; } catch (error) { out[name] = error.code; }
        }
        console.log(JSON.stringify(out));
    """))
    result = run_command(command="node", args_json='["work/probe.js"]')
    outcome = json.loads(result)
    # Opening the read-only placeholder for writing may report EROFS first.
    assert outcome.pop("vault_write") in {"EACCES", "EROFS"}
    assert outcome == {"vault_read": "EACCES", "state_read": "ok", "state_write": "EROFS", "notes_write": "ok"}
    after = _snapshot(workspace)
    assert after.pop("notes/node.txt")
    assert after == before


def test_symlink_and_case_aliases_resolve_to_the_protected_view(workspace):
    (workspace / "notes" / "vault-link").symlink_to("../.vault-key")
    (workspace / "notes" / "state-link").symlink_to("../extensions-state.json")
    (workspace / "vault-root-link").symlink_to(".vault-key")
    (workspace / ".VAULT-KEY").write_text("case variant secret\n")
    before = _snapshot(workspace)
    result = _python(workspace, """
        import json
        out = {}
        for name, action in {
            "notes_link_read": lambda: open("notes/vault-link").read(),
            "root_link_read": lambda: open("vault-root-link").read(),
            "case_variant_read": lambda: open(".VAULT-KEY").read(),
            "state_link_read": lambda: open("notes/state-link").read(),
            "state_link_write": lambda: open("notes/state-link", "a").close(),
        }.items():
            try:
                action()
                out[name] = "ok"
            except OSError as exc:
                out[name] = exc.errno
        print(json.dumps(out))
    """)
    outcome = json.loads(result["stdout"])
    assert outcome == {"notes_link_read": errno.EACCES, "root_link_read": errno.EACCES,
                       "case_variant_read": errno.EACCES, "state_link_read": "ok",
                       "state_link_write": errno.EROFS}
    assert _snapshot(workspace) == before


def test_denied_hard_link_alias_blocks_the_launch_with_its_path(workspace):
    os.link(workspace / ".vault-key", workspace / "notes" / "vault-hard-link")
    with pytest.raises(ValueError, match=r"process_isolation_unavailable:workspace_view_unsafe:hard_link_alias:\.vault-key"):
        run_command_raw("python3", "work/input.txt")
    (workspace / "notes" / "vault-hard-link").unlink()
    os.link(workspace / "artifacts" / "mail" / "private" / "inbox.eml", workspace / "notes" / "mail-alias")
    with pytest.raises(ValueError, match="hard_link_alias:artifacts/mail/private/inbox.eml"):
        run_command_raw("python3", "work/input.txt")


def run_command_raw(command: str, *args: str, cwd: str = "", timeout: int = 30) -> dict:
    return process_runtime_manager.run_command(command=command, args_json=json.dumps(list(args)), cwd=cwd,
                                               timeout_seconds=timeout)


def test_ordinary_project_work_is_unchanged(workspace):
    result = _python(workspace, """
        import json, os, subprocess
        out = {}
        out["notes_read"] = open("notes/a.txt").read()
        with open("notes/b.txt", "w") as handle:
            handle.write("beta\\n")
        out["sed_in_place"] = subprocess.run(["sed", "-i", "s/alpha/ALPHA/", "notes/a.txt"]).returncode
        os.makedirs("projects/demo/src", exist_ok=True)
        with open("soul.md", "a") as handle:
            handle.write("appended\\n")
        out["tmp_write"] = open("/tmp/scratch.txt", "w").write("x")
        out["home_write"] = open(os.path.join(os.environ["HOME"], "cache.txt"), "w").write("x")
        print(json.dumps(out))
    """)
    outcome = json.loads(result["stdout"])
    assert outcome["notes_read"] == "alpha\n" and outcome["sed_in_place"] == 0
    assert (workspace / "notes" / "a.txt").read_text() == "ALPHA\n"
    assert (workspace / "notes" / "b.txt").read_text() == "beta\n"
    assert (workspace / "projects" / "demo" / "src").is_dir()
    assert (workspace / "soul.md").read_text() == "soul\nappended\n"
    assert not (Path("/tmp") / "scratch.txt").exists() or (Path("/tmp") / "scratch.txt").read_text() != "x"


def test_root_level_rename_edits_fail_with_a_hint(workspace):
    result = _python(workspace, """
        import subprocess, sys
        completed = subprocess.run(["sed", "-i", "s/soul/SOUL/", "soul.md"], capture_output=True, text=True)
        sys.stderr.write(completed.stderr)
        sys.exit(completed.returncode)
    """)
    assert result["exit_code"] != 0
    assert "Read-only file system" in result["stderr"]
    assert result["workspace_view_hint"].startswith("(note: the workspace top level")
    assert (workspace / "soul.md").read_text() == "soul\n"


def test_git_pytest_and_runtimes_run_inside_the_view(workspace):
    project = workspace / "projects" / "app"
    project.mkdir(parents=True)
    (project / "test_ok.py").write_text("def test_ok():\n    assert 1 + 1 == 2\n")
    assert run_command_raw("git", "init", "-q", cwd="projects/app")["exit_code"] == 0
    assert run_command_raw("git", "add", "test_ok.py", cwd="projects/app")["exit_code"] == 0
    committed = run_command_raw("git", "-c", "user.name=Seraph", "-c", "user.email=seraph@example.invalid",
                                "commit", "-q", "-m", "init", cwd="projects/app")
    assert committed["exit_code"] == 0, committed
    assert "init" in run_command_raw("git", "log", "--oneline", cwd="projects/app")["stdout"]
    tested = run_command_raw("pytest", "-q", "-p", "no:cacheprovider", "test_ok.py", cwd="projects/app")
    assert tested["exit_code"] == 0 and "1 passed" in tested["stdout"], tested


def test_real_uv_and_npm_run_inside_the_view(workspace):
    executables = process_tools_module._sandbox_runtime()[1]
    if "uv" not in executables or "npm" not in executables:
        pytest.skip("uv and npm are both required for this parity check")
    assert run_command_raw("uv", "--version")["stdout"].startswith("uv ")
    venv = run_command_raw("uv", "venv", "--offline", "--no-config", "projects/env/.venv")
    assert venv["exit_code"] == 0, venv
    assert (workspace / "projects" / "env" / ".venv" / "pyvenv.cfg").is_file()
    web = workspace / "projects" / "web"
    web.mkdir(parents=True)
    (web / "package.json").write_text(json.dumps({"name": "web", "version": "0.0.0", "private": True}))
    assert run_command_raw("npm", "--version")["exit_code"] == 0
    # ``npm run``/``install`` stay refused by the network-capable argument
    # policy; ``pkg set`` and ``ls`` are real npm runs that read and write
    # the project inside the view.
    edited = run_command_raw("npm", "pkg", "set", "description=sandboxed", cwd="projects/web")
    assert edited["exit_code"] == 0, edited
    assert json.loads((web / "package.json").read_text())["description"] == "sandboxed"
    listed = run_command_raw("npm", "ls", "--offline", cwd="projects/web")
    assert listed["exit_code"] == 0 and "web@0.0.0" in listed["stdout"], listed


def _wheel(directory: Path) -> Path:
    files = {"seraphdemo/__init__.py": b"VALUE = 1\n",
             "seraphdemo-0.1.dist-info/METADATA": b"Metadata-Version: 2.1\nName: seraphdemo\nVersion: 0.1\n",
             "seraphdemo-0.1.dist-info/WHEEL": b"Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\nTag: py3-none-any\n"}
    record = [f"{name},sha256={base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b'=').decode()},{len(data)}"
              for name, data in files.items()] + ["seraphdemo-0.1.dist-info/RECORD,,"]
    wheel = directory / "seraphdemo-0.1-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        for name, data in files.items():
            archive.writestr(name, data)
        archive.writestr("seraphdemo-0.1.dist-info/RECORD", "\n".join(record) + "\n")
    return wheel


def test_real_uv_and_npm_extension_installs_do_not_false_positive(workspace, tmp_path):
    uv, npm = shutil.which("uv"), shutil.which("npm")
    if uv is None or npm is None:
        pytest.skip("uv and npm are both required for this parity check")
    # Installed on the host, as the runtime's extension lifecycle does; the
    # uv cache shares the filesystem, so files land as hard links (nlink 2).
    env = {**os.environ, "UV_CACHE_DIR": str(tmp_path / "uv-cache")}
    subprocess.run([uv, "pip", "install", "--offline", "--link-mode", "hardlink", "--target",
                    str(workspace / "extensions" / "py"), str(_wheel(tmp_path))], check=True, env=env,
                   capture_output=True)
    package = tmp_path / "pkg"
    package.mkdir()
    (package / "package.json").write_text('{"name":"seraph-demo","version":"0.1.0","main":"index.js"}')
    (package / "index.js").write_text("module.exports = 1\n")
    cache = str(tmp_path / "npm-cache")
    subprocess.run([npm, "pack", "--offline", "--cache", cache], cwd=package, check=True, capture_output=True)
    subprocess.run([npm, "install", "--offline", "--cache", cache, "--prefix", str(workspace / "extensions" / "js"),
                    str(package / "seraph-demo-0.1.0.tgz")], check=True, capture_output=True)
    installed = workspace / "extensions" / "py" / "seraphdemo" / "__init__.py"
    assert installed.stat().st_nlink > 1
    result = _python(workspace, """
        print(open("extensions/py/seraphdemo/__init__.py").read().strip())
        try:
            open("extensions/py/seraphdemo/__init__.py", "a")
        except OSError as exc:
            print(exc.errno)
    """)
    assert result["stdout"].split() == ["VALUE", "=", "1", str(errno.EROFS)]


def test_workspace_venv_uses_its_external_interpreter_read_only(workspace):
    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("uv is required to create a uv-managed workspace venv")
    project = workspace / "projects" / "venv-app"
    project.mkdir(parents=True)
    created = subprocess.run([uv, "venv", "--offline", "--no-config", str(project / ".venv")], capture_output=True,
                             text=True)
    if created.returncode != 0:
        pytest.skip("no interpreter uv can use offline: " + created.stderr[-200:])
    home = next(line.split("=", 1)[1].strip() for line in (project / ".venv" / "pyvenv.cfg").read_text().splitlines()
                if line.startswith("home"))
    result = _python(workspace, """
        import subprocess
        completed = subprocess.run([".venv/bin/python", "-c", "import sys; print(sys.base_prefix)"],
                                   capture_output=True, text=True)
        print(completed.returncode, completed.stdout.strip())
    """, cwd="projects/venv-app")
    code, base_prefix = result["stdout"].split()
    assert code == "0" and str(Path(home).parent) == base_prefix


def test_workspace_venv_inside_a_forbidden_tree_is_refused(tmp_path):
    forbidden = tmp_path / "private"
    install = forbidden / "python"
    (install / "bin").mkdir(parents=True)
    (install / "lib" / "python3.12").mkdir(parents=True)
    (install / "lib" / "python3.12" / "os.py").write_text("")
    (install / "bin" / "python3").write_text("")
    workspace = tmp_path / "workspace"
    (workspace / ".venv").mkdir(parents=True)
    (workspace / ".venv" / "pyvenv.cfg").write_text(f"home = {install / 'bin'}\n")
    with pytest.raises(process_isolation.ProcessIsolationBlocked, match="runtime_root_unsafe"):
        process_isolation.workspace_venv_runtime_paths(workspace, workspace, forbidden_ancestors=(),
                                                       forbidden_trees=(str(forbidden),))
    found = process_isolation.workspace_venv_runtime_paths(workspace, workspace, forbidden_ancestors=(),
                                                           forbidden_trees=())
    assert install / "lib" in found


def test_capabilities_network_and_host_files_inside_the_sandbox(workspace):
    result = _python(workspace, """
        import json, os, socket
        status = dict(line.split(":", 1) for line in open("/proc/self/status") if ":" in line)
        print(json.dumps({"cap_eff": status["CapEff"].strip(), "cap_bnd": status["CapBnd"].strip(),
                          "interfaces": sorted(name for _, name in socket.if_nameindex()),
                          "home_ssh": os.path.exists(os.path.expanduser("~/.ssh")),
                          "host_home": os.path.exists(%r), "uid": os.getuid()}))
    """ % str(Path.home() / ".ssh"))
    observed = json.loads(result["stdout"])
    assert observed["cap_eff"] == observed["cap_bnd"] == "0000000000000000"
    assert observed["interfaces"] == ["lo"]
    assert observed["home_ssh"] is False and observed["host_home"] is False
    assert result["network_namespace"] == "loopback_only"
    assert result["host_paths_outside_workspace_visible"] is False


def _wait_for(path: Path, timeout: float = 10) -> None:
    deadline = time.monotonic() + timeout
    while not path.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert path.exists(), f"{path} did not appear"


def test_start_process_shares_the_host_network_and_says_so(workspace):
    relative = _script(workspace, "interfaces.py", """
        import json, socket
        with open("notes/interfaces.json", "w") as handle:
            json.dump(sorted(name for _, name in socket.if_nameindex()), handle)
    """)
    payload = process_runtime_manager.start_process(command="python3", args_json=json.dumps([relative]))
    assert payload["network_namespace"] == "host_shared"
    _wait_for(workspace / "notes" / "interfaces.json")
    time.sleep(0.1)
    assert json.loads((workspace / "notes" / "interfaces.json").read_text()) == sorted(
        name for _, name in socket.if_nameindex())
    context = start_process.get_approval_context({"command": "python3"})
    assert process_isolation.LOCALHOST_NOTE in context["approval_notes"]
    assert context["network_namespace"] == "host_shared"
    run_context = run_command.get_approval_context({"command": "python3"})
    assert process_isolation.LOCALHOST_NOTE not in run_context["approval_notes"]
    assert run_context["network_namespace"] == "loopback_only"


def _heartbeat_script(root: Path, *, ignore_term: bool) -> str:
    return _script(root, f"daemon_{int(ignore_term)}.py", f"""
        import os, signal, sys, time
        if {ignore_term!r}:
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
        else:
            signal.signal(signal.SIGTERM, lambda *_: (open("notes/graceful", "w").close(), sys.exit(0)))
        if os.fork() == 0:
            os.setsid()
            if os.fork() == 0:
                while True:
                    with open("notes/heartbeat", "w") as handle:
                        handle.write(str(time.monotonic()))
                    time.sleep(0.05)
            os._exit(0)
        open("notes/ready", "w").close()
        while True:
            time.sleep(0.1)
    """)


def _heartbeat_stops(path: Path) -> bool:
    first = path.read_text()
    time.sleep(0.4)
    return path.read_text() == first


@pytest.mark.parametrize("ignore_term", [False, True], ids=["sigterm", "sigterm-then-sigkill"])
def test_stop_tears_down_a_double_forked_daemon(workspace, ignore_term):
    relative = _heartbeat_script(workspace, ignore_term=ignore_term)
    payload = process_runtime_manager.start_process(command="python3", args_json=json.dumps([relative]))
    _wait_for(workspace / "notes" / "ready")
    _wait_for(workspace / "notes" / "heartbeat")
    stopped = process_runtime_manager.stop_process(payload["process_id"])
    assert stopped["cleanup_status"] == "stopped" and stopped["registry_removed"] is True
    assert (workspace / "notes" / "graceful").exists() is (not ignore_term)
    assert _heartbeat_stops(workspace / "notes" / "heartbeat")


def test_start_process_outlives_its_short_lived_launching_thread(workspace):
    """--die-with-parent is a thread-scoped PDEATHSIG (#1103 review).

    Tool calls run on short-lived worker threads; the sandbox must survive
    the thread that requested it and end only when stopped (or with the
    backend).
    """
    import threading

    relative = _heartbeat_script(workspace, ignore_term=False)
    launched = {}
    worker = threading.Thread(target=lambda: launched.update(
        process_runtime_manager.start_process(command="python3", args_json=json.dumps([relative]))))
    worker.start()
    worker.join(timeout=30)
    assert not worker.is_alive() and launched["process_id"]
    _wait_for(workspace / "notes" / "heartbeat")
    time.sleep(1.0)  # well past the launching thread's exit
    assert not _heartbeat_stops(workspace / "notes" / "heartbeat")
    listed = {item["process_id"]: item for item in process_runtime_manager.list_processes()}
    assert listed[launched["process_id"]]["status"] == "running"
    stopped = process_runtime_manager.stop_process(launched["process_id"])
    assert stopped["cleanup_status"] == "stopped"
    assert _heartbeat_stops(workspace / "notes" / "heartbeat")


def test_timeout_tears_down_a_double_forked_daemon(workspace):
    relative = _heartbeat_script(workspace, ignore_term=True)
    result = process_runtime_manager.run_command(command="python3", args_json=json.dumps([relative]),
                                                 timeout_seconds=2)
    assert result["timed_out"] is True and result["cleanup_status"] == "stopped"
    assert _heartbeat_stops(workspace / "notes" / "heartbeat")


def test_stop_all_processes_reaps_children_before_a_workspace_swap(workspace):
    relative = _heartbeat_script(workspace, ignore_term=True)
    process_runtime_manager.start_process(command="python3", args_json=json.dumps([relative]))
    _wait_for(workspace / "notes" / "heartbeat")
    from src.workspace import lifecycle

    with lifecycle._agent_processes_fenced(strict=True):
        assert process_runtime_manager.list_all_processes() == []
        assert _heartbeat_stops(workspace / "notes" / "heartbeat")
        with pytest.raises(ValueError, match="workspace maintenance is in progress"):
            run_command_raw("git", "--version")
    assert run_command_raw("git", "--version")["exit_code"] == 0


def test_root_level_files_are_bound_by_inode_so_a_host_rename_leaves_the_old_file(workspace):
    relative = _script(workspace, "watch.py", """
        import time
        open("notes/watch-ready", "w").close()
        for _ in range(100):
            if open("notes/go").read() if __import__("os").path.exists("notes/go") else "":
                break
            time.sleep(0.05)
        with open("notes/seen.txt", "w") as handle:
            handle.write(open("soul.md").read())
        try:
            __import__("os").replace("notes/a.txt", "soul.md")
            handle_result = "renamed"
        except OSError as exc:
            handle_result = str(exc.errno)
        open("notes/rename-result", "w").write(handle_result)
    """)
    process_runtime_manager.start_process(command="python3", args_json=json.dumps([relative]))
    _wait_for(workspace / "notes" / "watch-ready")
    replacement = workspace / "soul.md.new"
    replacement.write_text("replaced by seraph\n")
    os.replace(replacement, workspace / "soul.md")
    (workspace / "notes" / "go").write_text("go")
    _wait_for(workspace / "notes" / "rename-result")
    assert (workspace / "notes" / "seen.txt").read_text() == "soul\n"
    # A rename onto the fd-bound file is refused (read-only level, bind
    # mount busy, or cross-mount): the host file is never replaced from inside.
    assert (workspace / "notes" / "rename-result").read_text() in {
        str(errno.EROFS), str(errno.EBUSY), str(errno.EXDEV)}
    assert (workspace / "soul.md").read_text() == "replaced by seraph\n"
    assert process_isolation.STALE_INODE_NOTE in run_command.get_approval_context({"command": "git"})["approval_notes"]


# ----------------------------------------------------------- view safety


def test_synthetic_levels_are_exactly_the_reviewed_read_only_levels(tmp_path):
    """CI guard: a new protected declaration must not silently widen the read-only levels."""
    registry = canonical_workspace_registry(tmp_path)
    protected = [logical for logical, _access in registry.agent_protected_paths()]
    protected += list(process_tools_module._SANDBOX_DENIED_NAMES)
    assert process_isolation.synthetic_levels(protected) == {".", "artifacts", "artifacts/mail"}


def test_too_many_top_level_entries_block_the_launch(workspace):
    for index in range(process_isolation.MAX_LEVEL_CHILDREN + 1):
        (workspace / f"entry-{index:04d}").write_text("")
    with pytest.raises(ValueError, match="workspace_view_unsafe:too_many_entries"):
        run_command_raw("git", "--version")


@pytest.mark.parametrize(("target", "allowed"), [
    ("/etc/passwd", False), ("..", False), ("notes/../..", False), ("notes", True), ("notes/missing", True),
    (".vault-key", True),
])
def test_level_symlinks_are_recreated_only_inside_the_view(workspace, target, allowed):
    (workspace / "link").symlink_to(target)
    if allowed:
        assert run_command_raw("git", "--version")["exit_code"] == 0
    else:
        with pytest.raises(ValueError, match="workspace_view_unsafe:symlink_escapes_view:link"):
            run_command_raw("git", "--version")


def test_absolute_symlink_into_the_workspace_is_allowed(workspace):
    (workspace / "link").symlink_to(workspace / "notes")
    assert run_command_raw("git", "--version")["exit_code"] == 0


def test_synthetic_level_that_is_not_a_directory_blocks(workspace):
    shutil.rmtree(workspace / "artifacts")
    (workspace / "artifacts").write_text("not a directory")
    with pytest.raises(ValueError, match="synthetic_level_not_directory:artifacts"):
        run_command_raw("git", "--version")


def test_cwd_inside_denied_state_is_refused(workspace):
    with pytest.raises(ValueError, match="runtime-owned"):
        run_command_raw("git", "--version", cwd="artifacts/mail/private")


def test_view_construction_never_follows_a_swapped_level(workspace, monkeypatch):
    """Every view operation resolves beneath one root fd and never through a symlink."""
    calls = []
    original = process_isolation.openat2

    def observe(dirfd, path, flags, resolve):
        if dirfd >= 0:  # AT_FDCWD is only the openat2 availability check
            calls.append((path, resolve))
        return original(dirfd, path, flags, resolve)

    monkeypatch.setattr(process_isolation, "openat2", observe)
    run_command_raw("git", "--version")
    assert calls
    assert all(resolve & process_isolation.RESOLVE_BENEATH and resolve & process_isolation.RESOLVE_NO_MAGICLINKS
               for _path, resolve in calls)


# ------------------------------------------------- posture and fail-closed


def _unavailable(reason: str):
    return process_isolation.ProcessIsolationPosture("unavailable", reason=reason)


@pytest.mark.parametrize("reason", sorted(process_isolation.HOST_REASONS))
def test_unavailable_hosts_are_blocked_with_a_typed_reason_and_no_child(workspace, monkeypatch, reason):
    monkeypatch.setattr(process_tools_module, "_sandbox_posture", lambda: _unavailable(reason))
    monkeypatch.setattr(process_tools_module, "_spawn_process", lambda *a, **k: pytest.fail("launched"))
    with pytest.raises(ValueError, match=f"^process_isolation_unavailable:{reason}"):
        run_command_raw("git", "--version")
    with pytest.raises(ValueError, match=f"^process_isolation_unavailable:{reason}"):
        process_runtime_manager.start_process(command="git", args_json='["--version"]')
    assert run_command(command="git", args_json='["--version"]').startswith(
        f"Error: process_isolation_unavailable:{reason}")
    assert run_command.get_approval_preflight_error({}).startswith(f"Error: process_isolation_unavailable:{reason}")


def test_blocked_host_returns_before_any_approval_is_created(async_db, workspace, monkeypatch):
    from unittest.mock import patch

    from src.approval.repository import approval_repository
    from src.approval.runtime import reset_runtime_context, set_runtime_context
    from src.security.trust_contract import AuthorityGrant, PrincipalType, TrustPrincipal
    from src.tools.approval import wrap_tools_with_forced_approval

    monkeypatch.setattr(process_tools_module, "_sandbox_posture", lambda: _unavailable("userns_denied"))
    tool = wrap_tools_with_forced_approval([run_command])[0]
    principal = TrustPrincipal(principal_id="operator:test", principal_type=PrincipalType.OPERATOR,
                               grants=(AuthorityGrant.CAPABILITY_EXECUTE,), session_id="s1")
    tokens = set_runtime_context("s1", "high_risk", trust_principal=principal)
    try:
        with patch.object(approval_repository, "get_or_create_pending") as pending:
            result = tool(command="git", args_json='["--version"]')
    finally:
        reset_runtime_context(tokens)
    pending.assert_not_called()
    assert result.startswith("Error: process_isolation_unavailable:userns_denied")


def test_approval_context_binds_the_posture_and_describes_the_view(workspace):
    context = run_command.get_approval_context({"command": "git", "args_json": '["status"]'})
    posture = process_tools_module._sandbox_posture()
    assert context["process_isolation_posture_sha256"] == posture.digest
    assert context["process_isolation_variant"] == "isolated"
    assert context["workspace_view"] == "os_enforced"
    assert context["workspace_view_read_only_levels"] == [".", "artifacts", "artifacts/mail"]
    assert context["host_paths_outside_workspace_visible"] is False
    assert any(note.startswith("Runtime-owned workspace state is unreadable (") for note in context["approval_notes"])


def test_posture_change_between_approval_and_launch_is_blocked(workspace, monkeypatch):
    token = capability_execution._APPROVED_CONTEXT.set({"process_isolation_posture_sha256": "0" * 64})
    try:
        with monkeypatch.context() as patched:
            patched.setattr(process_tools_module, "_spawn_process", lambda *a, **k: pytest.fail("launched"))
            with pytest.raises(ValueError, match="^process_isolation_posture_changed"):
                run_command_raw("git", "--version")
    finally:
        capability_execution._APPROVED_CONTEXT.reset(token)
    posture = process_tools_module._sandbox_posture()
    token = capability_execution._APPROVED_CONTEXT.set({"process_isolation_posture_sha256": posture.digest})
    try:
        assert run_command_raw("git", "--version")["exit_code"] == 0
    finally:
        capability_execution._APPROVED_CONTEXT.reset(token)


def test_guard_mismatch_aborts_before_the_command_runs(workspace, monkeypatch):
    original = process_isolation.build_workspace_view

    def tampered(*args, **kwargs):
        view = original(*args, **kwargs)
        view.denied = (*view.denied, str(workspace / "notes" / "a.txt"))
        return view

    monkeypatch.setattr(process_isolation, "build_workspace_view", tampered)
    relative = _script(workspace, "marker.py", "open('notes/ran', 'w').close()\n")
    with pytest.raises(ValueError, match="workspace_view_mismatch:denied_readable:.*notes/a.txt"):
        run_command_raw("python3", relative)
    assert not (workspace / "notes" / "ran").exists()


def test_sandbox_startup_failure_is_never_a_command_result(workspace, monkeypatch, tmp_path):
    fake = tmp_path / "bwrap"
    fake.write_text("#!/bin/sh\necho 'bwrap: Creating new namespace failed: Operation not permitted' >&2\nexit 1\n")
    fake.chmod(0o755)
    real = process_tools_module._sandbox_posture()
    monkeypatch.setattr(process_tools_module, "_sandbox_posture",
                        lambda: process_isolation.ProcessIsolationPosture(**{**real.__dict__, "bwrap_path": str(fake)}))
    with pytest.raises(ValueError, match="^process_isolation_unavailable:sandbox_startup_failed"):
        run_command_raw("git", "--version")
    with pytest.raises(ValueError, match="^process_isolation_unavailable:sandbox_startup_failed"):
        process_runtime_manager.start_process(command="git", args_json='["--version"]')
    assert process_runtime_manager.list_all_processes() == []


def _fake_bwrap(tmp_path: Path, stderr: str) -> Path:
    fake = tmp_path / "bin" / "bwrap"
    fake.parent.mkdir()
    (tmp_path / "bin" / "help.txt").write_text(" ".join(process_isolation.REQUIRED_BWRAP_FLAGS) + "\n")
    (tmp_path / "bin" / "stderr.txt").write_text(stderr + "\n")
    fake.write_text(f"#!/bin/sh\nif [ \"$1\" = --help ]; then cat {tmp_path}/bin/help.txt; exit 0; fi\n"
                    f"cat {tmp_path}/bin/stderr.txt >&2\nexit 1\n")
    fake.chmod(0o755)
    return fake


@pytest.mark.parametrize(("stderr", "container", "reason"), [
    ("bwrap: setting up uid map: Permission denied", False, "userns_denied"),
    ("bwrap: No permissions to create new namespace", True, "container_seccomp_profile_missing"),
    ("bwrap: Can't mount proc on /newroot/proc: Operation not permitted", True, "container_proc_masked"),
    ("bwrap: Failed to mount tmpfs: Permission denied", True, "container_apparmor_denies_mount"),
    ("bwrap: something unexpected", False, "probe_failed"),
])
def test_probe_types_host_failures(tmp_path, monkeypatch, stderr, container, reason):
    fake = _fake_bwrap(tmp_path, stderr)
    monkeypatch.setenv("PATH", f"{fake.parent}:{os.environ['PATH']}")
    monkeypatch.setattr(process_isolation, "in_container", lambda: container)
    runtime = process_isolation.process_runtime({})
    posture = process_isolation.probe(runtime, use_cache=False)
    assert posture.isolated is False and posture.reason == reason


def test_probe_requires_every_flag_the_sandbox_uses(tmp_path, monkeypatch):
    fake = _fake_bwrap(tmp_path, "unused")
    monkeypatch.setenv("PATH", f"{fake.parent}:{os.environ['PATH']}")
    monkeypatch.setattr(process_isolation, "bwrap_flags",
                        lambda _path: frozenset(process_isolation.REQUIRED_BWRAP_FLAGS) - {"--ro-bind-fd"})
    assert process_isolation.probe(process_isolation.process_runtime({}), use_cache=False).reason == "bwrap_unsupported"


def test_probe_reports_missing_bwrap_and_unsupported_platforms(monkeypatch):
    runtime = process_isolation.process_runtime({})
    monkeypatch.setattr(process_isolation.shutil, "which", lambda _name: None)
    assert process_isolation.probe(runtime, use_cache=False).reason == "bwrap_missing"
    monkeypatch.setattr(process_isolation, "sys", SimpleNamespace(platform="darwin"))
    assert process_isolation.probe(runtime, use_cache=False).reason == "platform_unsupported"


def test_probe_refuses_runtime_roots_that_expose_the_workspace(tmp_path):
    runtime = process_isolation.process_runtime({})
    posture = process_isolation.probe(runtime, forbidden_trees=[str(path) for path in runtime.read_only_paths[:1]],
                                      use_cache=False)
    assert posture.reason == "runtime_root_unsafe"


def test_probe_is_isolated_on_a_capable_host():
    runtime = process_isolation.process_runtime({})
    posture = process_isolation.probe(runtime, hidden_paths=process_isolation.host_secret_paths(None), use_cache=False)
    if not posture.isolated:
        isolation_unavailable(posture.reason)
    assert posture.bwrap_sha256 and posture.probe_sha256 and len(posture.digest) == 64
