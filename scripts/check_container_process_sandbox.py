#!/usr/bin/env python3
"""Build the backend image and probe the agent process sandbox inside it (#1103).

Positive check: the container started with the shipped compose security
options (seccomp profile, AppArmor profile, systempaths=unconfined,
no-new-privileges, cap_drop ALL) must report an isolated posture and run a
real command through the process tools with DENIED state refused.

Negative check: the same image under Docker's defaults must report a typed
``process_isolation_unavailable:<reason>``, never an isolated posture.

``--compare-seccomp PATH`` additionally checks that every rule of the shipped
profile that is not tagged ``seraph #1103`` appears unchanged in Docker's
default profile at PATH (so the base grants nothing broader than Docker).

Without a reachable Docker daemon the check is skipped with a reason (exit 0),
unless ``--require-docker`` is given. No network is used except by
``docker build`` itself.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
PROFILE = ROOT / "deploy" / "seccomp" / "seraph-backend.json"
COMPOSE = ROOT / "docker-compose.prod.yaml"
SERVICE = "backend-prod"
TYPED_CONTAINER_REASONS = {"container_seccomp_profile_missing", "container_apparmor_denies_mount",
                           "container_proc_masked", "userns_denied"}

PROBE = r"""
import json, os, tempfile
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


def docker_options(security_opt: list[str], cap_drop: list[str]) -> list[str]:
    options: list[str] = []
    for item in security_opt:
        if item.startswith("seccomp="):
            item = "seccomp=" + str((ROOT / item.split("=", 1)[1]).resolve())
        options += ["--security-opt", item]
    for item in cap_drop:
        options += ["--cap-drop", item]
    return options


def compare_seccomp(upstream_path: Path) -> list[str]:
    ours = json.loads(PROFILE.read_text())
    upstream = json.loads(upstream_path.read_text())

    def key(rule: dict) -> str:
        return json.dumps({k: v for k, v in rule.items() if k not in {"names", "comment"}}, sort_keys=True)

    granted: dict[str, set[str]] = {}
    for rule in upstream["syscalls"]:
        for name in rule["names"]:
            granted.setdefault(key(rule), set()).add(name)
    problems = []
    for rule in ours["syscalls"]:
        if str(rule.get("comment", "")).startswith("seraph #1103"):
            continue
        extra = set(rule["names"]) - granted.get(key(rule), set())
        if extra:
            problems.append(f"{sorted(extra)} under {key(rule)}")
    if ours["defaultAction"] != upstream["defaultAction"]:
        problems.append("defaultAction differs")
    return problems


def run(command: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(command, cwd=ROOT, text=True, **kwargs)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", default="seraph-backend:process-sandbox-check")
    parser.add_argument("--skip-build", action="store_true")
    parser.add_argument("--require-docker", action="store_true")
    parser.add_argument("--compare-seccomp", type=Path)
    parser.add_argument("--compare-only", action="store_true")
    args = parser.parse_args(argv)
    receipt: dict = {"schema": "seraph.container-process-sandbox-check.v1"}
    status = 0
    if args.compare_seccomp is not None:
        problems = compare_seccomp(args.compare_seccomp)
        receipt["seccomp_base_matches_docker_default"] = not problems
        receipt["seccomp_base_extras"] = problems
        status |= bool(problems)
        if args.compare_only:
            print(json.dumps(receipt, indent=2))
            return status
    if run(["docker", "info"], capture_output=True).returncode != 0:
        receipt["skipped"] = "no reachable Docker daemon"
        print(json.dumps(receipt, indent=2))
        return 1 if args.require_docker else status
    security_opt, cap_drop = compose_security(COMPOSE.read_text(), SERVICE)
    receipt["compose_security_opt"], receipt["compose_cap_drop"] = security_opt, cap_drop
    if not args.skip_build:
        built = run(["docker", "build", "--build-context", "cordis=runtime/cordis", "-t", args.image, "backend"])
        if built.returncode != 0:
            receipt["build"] = "failed"
            print(json.dumps(receipt, indent=2))
            return 1
    probe_command = ["/app/.venv/bin/python", "-c", PROBE]
    shipped = run(["docker", "run", "--rm", *docker_options(security_opt, cap_drop), args.image, *probe_command],
                  capture_output=True)
    receipt["shipped_profile"] = _decode(shipped)
    positive = receipt["shipped_profile"]
    command = positive.get("command") or {}
    if not (positive.get("variant") == "isolated" and positive.get("uid") == 10001
            and command.get("exit_code") == 0 and command.get("stdout") == '{"vault": 13}'
            and command.get("notes_written") is True):
        status = 1
    default = run(["docker", "run", "--rm", "--cap-drop", "ALL", args.image, *probe_command], capture_output=True)
    receipt["docker_defaults"] = _decode(default)
    if not (receipt["docker_defaults"].get("variant") == "unavailable"
            and receipt["docker_defaults"].get("reason") in TYPED_CONTAINER_REASONS):
        status = 1
    receipt["status"] = "passed" if status == 0 else "failed"
    print(json.dumps(receipt, indent=2))
    return status


def _decode(completed: subprocess.CompletedProcess) -> dict:
    try:
        return json.loads(completed.stdout.strip().splitlines()[-1])
    except (IndexError, ValueError):
        return {"exit_code": completed.returncode, "stderr": completed.stderr[-2000:]}


if __name__ == "__main__":
    sys.exit(main())
