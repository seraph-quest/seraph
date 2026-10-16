#!/usr/bin/env python3
"""Build the backend image and probe the agent process sandbox inside it (#1103).

Positive check (``docker compose run`` on the production service, so the
compose file's relative seccomp path, security options, ``cap_drop`` and the
image's non-root user are exactly what is exercised; the AppArmor override is
added when the ``seraph-backend`` profile is loaded): the posture must be
isolated, a real command through the process tools must have ``.vault-key``
refused while ``notes/`` stays writable, the user must be uid 10001 and
``uv run`` must work as that user.

Negative check (``docker run`` of the same image under Docker's defaults):
the posture must be a typed ``process_isolation_unavailable:<reason>``.

``--compare-seccomp PATH`` checks that every rule of the vendored base
``deploy/seccomp/docker-default.json`` appears unchanged in Docker's default
profile at PATH, so the shipped profile grants nothing broader than Docker
beyond its ``seraph #1103`` rules.

Without a reachable Docker daemon the check is skipped with a reason (exit 0),
unless ``--require-docker`` is given.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
BASE_PROFILE = ROOT / "deploy" / "seccomp" / "docker-default.json"
COMPOSE = ROOT / "docker-compose.prod.yaml"
APPARMOR_OVERRIDE = ROOT / "docker-compose.prod.apparmor.yaml"
SERVICE = "backend-prod"
PROJECT = "seraph-prod"
TYPED_CONTAINER_REASONS = {"container_seccomp_profile_missing", "container_apparmor_denies_mount",
                           "container_proc_masked", "userns_denied"}

PROBE = r"""
import json, os, subprocess, tempfile
from pathlib import Path
from src.execution import process_isolation as sandbox
posture = sandbox.probe(sandbox.process_runtime({}), hidden_paths=sandbox.host_secret_paths(None), use_cache=False)
receipt = {"variant": posture.variant, "reason": posture.reason, "in_container": posture.in_container,
           "uid": os.getuid()}
if posture.isolated:
    workspace = Path(tempfile.mkdtemp(prefix="sandbox-check-")).resolve()
    (workspace / ".vault-key").write_text("secret")
    (workspace / "notes").mkdir()
    (workspace / "notes" / "probe.py").write_text(
        "import json\ntry:\n    open('.vault-key').read(); v='readable'\nexcept OSError as e:\n    v=e.errno\n"
        "open('notes/ok.txt','w').write('ok')\nprint(json.dumps({'vault': v}))\n")
    os.environ["WORKSPACE_DIR"] = str(workspace)
    from config.settings import settings
    settings.workspace_dir = str(workspace)
    from src.tools.process_tools import process_runtime_manager
    result = process_runtime_manager.run_command(command="python3", args_json='["notes/probe.py"]')
    receipt["command"] = {"exit_code": result["exit_code"], "stdout": result["stdout"].strip(),
                          "notes_written": (workspace / "notes" / "ok.txt").is_file()}
print(json.dumps(receipt))
"""


def compose_security(compose_text: str, service: str) -> tuple[list[str], list[str]]:
    """security_opt and cap_drop entries of one service (no YAML dependency)."""
    block = compose_text.split(f"\n  {service}:\n", 1)[1]
    block = re.split(r"\n  [A-Za-z0-9_-]+:\n|\nnetworks:\n", block, maxsplit=1)[0]

    def entries(key: str) -> list[str]:
        match = re.search(rf"\n    {key}:\n((?:      - .*\n)+)", block + "\n")
        return [line.strip()[2:].strip().strip('"') for line in match.group(1).splitlines()] if match else []

    return entries("security_opt"), entries("cap_drop")


def apparmor_profile_loaded() -> bool:
    enabled = Path("/sys/module/apparmor/parameters/enabled")
    profiles = Path("/sys/kernel/security/apparmor/policy/profiles")
    try:
        return (enabled.read_text().strip() == "Y"
                and any(entry.name.startswith("seraph-backend.") for entry in profiles.iterdir()))
    except OSError:
        return False


def compare_seccomp(upstream_path: Path, base_path: Path = BASE_PROFILE) -> list[str]:
    """Rules of the vendored base that Docker's default profile does not grant identically."""
    ours = json.loads(base_path.read_text())
    upstream = json.loads(upstream_path.read_text())

    def key(rule: dict) -> str:
        return json.dumps({k: v for k, v in rule.items() if k not in {"names", "comment"}}, sort_keys=True)

    granted: dict[str, set[str]] = {}
    for rule in upstream["syscalls"]:
        granted.setdefault(key(rule), set()).update(rule["names"])
    problems = []
    for rule in ours["syscalls"]:
        extra = set(rule["names"]) - granted.get(key(rule), set())
        if extra:
            problems.append(f"{sorted(extra)} under {key(rule)}")
    for field in ("defaultAction", "defaultErrnoRet"):
        if ours.get(field) != upstream.get(field):
            problems.append(f"{field} differs")
    return problems


def run(command: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(command, cwd=ROOT, text=True, **kwargs)


def _decode(completed: subprocess.CompletedProcess) -> dict:
    try:
        return json.loads(completed.stdout.strip().splitlines()[-1])
    except (IndexError, ValueError):
        return {"exit_code": completed.returncode, "stdout": completed.stdout[-1000:],
                "stderr": completed.stderr[-2000:]}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skip-build", action="store_true")
    parser.add_argument("--require-docker", action="store_true")
    parser.add_argument("--compare-seccomp", type=Path)
    parser.add_argument("--compare-only", action="store_true")
    args = parser.parse_args(argv)
    receipt: dict = {"schema": "seraph.container-process-sandbox-check.v2"}
    status = 0
    if args.compare_seccomp is not None:
        problems = compare_seccomp(args.compare_seccomp)
        receipt["seccomp_base_within_docker_default"] = not problems
        receipt["seccomp_base_extras"] = problems
        status |= bool(problems)
        if args.compare_only:
            print(json.dumps(receipt, indent=2))
            return status
    if run(["docker", "info"], capture_output=True).returncode != 0:
        receipt["skipped"] = "no reachable Docker daemon"
        print(json.dumps(receipt, indent=2))
        return 1 if args.require_docker else status
    env_file = ROOT / ".env.prod"
    created_env = not env_file.exists()
    if created_env:
        # The service's env_file must exist; the example holds no secrets.
        env_file.write_text((ROOT / "env.prod.example").read_text())
    try:
        files = ["-f", str(COMPOSE)]
        receipt["apparmor_profile_loaded"] = apparmor_profile_loaded()
        if receipt["apparmor_profile_loaded"]:
            files += ["-f", str(APPARMOR_OVERRIDE)]
        compose = ["docker", "compose", "-p", PROJECT, *files]
        receipt["compose_security_opt"], receipt["compose_cap_drop"] = compose_security(COMPOSE.read_text(), SERVICE)
        if not args.skip_build and run([*compose, "build", SERVICE]).returncode != 0:
            receipt["build"] = "failed"
            print(json.dumps(receipt, indent=2))
            return 1
        compose_run = [*compose, "run", "--rm", "--no-deps", "-T", SERVICE]
        shipped = run([*compose_run, "/app/.venv/bin/python", "-c", PROBE], capture_output=True)
        receipt["shipped_profile"] = positive = _decode(shipped)
        command = positive.get("command") or {}
        if not (positive.get("variant") == "isolated" and positive.get("uid") == 10001
                and command.get("exit_code") == 0 and command.get("stdout") == '{"vault": 13}'
                and command.get("notes_written") is True):
            status = 1
        uv = run([*compose_run, "uv", "run", "python", "-c", "import os, sys; print(os.getuid(), sys.prefix)"],
                 capture_output=True)
        receipt["uv_run_as_runtime_user"] = {"exit_code": uv.returncode, "stdout": uv.stdout.strip()[-200:],
                                             "stderr": uv.stderr.strip()[-500:]}
        if uv.returncode != 0 or not uv.stdout.strip().startswith("10001 "):
            status = 1
        image = f"{PROJECT}-{SERVICE}"
        default = run(["docker", "run", "--rm", "--cap-drop", "ALL", image, "/app/.venv/bin/python", "-c", PROBE],
                      capture_output=True)
        receipt["docker_defaults"] = _decode(default)
        if not (receipt["docker_defaults"].get("variant") == "unavailable"
                and receipt["docker_defaults"].get("reason") in TYPED_CONTAINER_REASONS):
            status = 1
    finally:
        if created_env:
            env_file.unlink(missing_ok=True)
    receipt["status"] = "passed" if status == 0 else "failed"
    print(json.dumps(receipt, indent=2))
    return status


if __name__ == "__main__":
    sys.exit(main())
