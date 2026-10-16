"""Docker deployment of the agent process sandbox (#1103).

CI-safe static checks of the compose files, the image, the shipped seccomp
and AppArmor profiles and the container preflight.  The image build and
in-container probe (``scripts/check_container_process_sandbox.py``) runs only
when a Docker daemon is reachable and ``SERAPH_CONTAINER_SANDBOX_CHECK=1``.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import sys

import pytest
import yaml

import production_preflight
from src.execution import process_isolation
from src.workspace.production import workspace_ownership_problem

REPO = Path(__file__).resolve().parents[2]
PROFILE = REPO / "deploy" / "seccomp" / "seraph-backend.json"
APPARMOR = REPO / "deploy" / "apparmor" / "seraph-backend"
EXPECTED_SECURITY_OPT = [
    "seccomp=./deploy/seccomp/seraph-backend.json",
    "apparmor=seraph-backend",
    "systempaths=unconfined",
    "no-new-privileges:true",
]
CLONE_NEWCGROUP = 0x02000000
CLONE_NEWTIME = 0x80
DOCKER_DEFAULT_NAMESPACE_MASK = 2114060288


@pytest.mark.parametrize(("name", "service"), [("dev", "backend-dev"), ("prod", "backend-prod")])
def test_compose_backend_uses_the_shipped_profiles_and_drops_all_capabilities(name, service):
    backend = yaml.safe_load((REPO / f"docker-compose.{name}.yaml").read_text())["services"][service]
    assert backend["security_opt"] == EXPECTED_SECURITY_OPT
    assert backend["cap_drop"] == ["ALL"]
    assert "cap_add" not in backend and not backend.get("privileged")
    assert (REPO / backend["security_opt"][0].split("=", 1)[1]).resolve() == PROFILE


def test_check_script_reads_the_same_compose_security_options():
    sys.path.insert(0, str(REPO / "scripts"))
    try:
        import check_container_process_sandbox as script
    finally:
        sys.path.remove(str(REPO / "scripts"))
    security_opt, cap_drop = script.compose_security((REPO / "docker-compose.prod.yaml").read_text(), "backend-prod")
    assert security_opt == EXPECTED_SECURITY_OPT and cap_drop == ["ALL"]
    options = script.docker_options(security_opt, cap_drop)
    assert f"seccomp={PROFILE}" in options and options[-2:] == ["--cap-drop", "ALL"]


def test_image_runs_as_the_fixed_non_root_user_with_bubblewrap():
    dockerfile = (REPO / "backend" / "Dockerfile").read_text()
    final = dockerfile[dockerfile.index("FROM ghcr.io/astral-sh/uv"):]
    assert re.search(r"^RUN apt install .*\bbubblewrap\b", final, re.M)
    assert "--uid 10001 --gid 10001" in final and "--gid 10001 seraph" in final
    user_lines = re.findall(r"^USER (\S+)$", final, re.M)
    assert user_lines == ["seraph"]
    # No later instruction runs as root, and the runtime command follows.
    after_user = final[final.index("USER seraph"):]
    assert not re.search(r"^RUN ", after_user, re.M) and re.search(r"^CMD ", after_user, re.M)
    assert final.index("ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright") < final.index("playwright install chromium")
    assert "chown -R 10001:10001 /app/data /app/logs" in final


def _rules():
    profile = json.loads(PROFILE.read_text())
    seraph = [rule for rule in profile["syscalls"] if str(rule.get("comment", "")).startswith("seraph #1103")]
    base = [rule for rule in profile["syscalls"] if rule not in seraph]
    return profile, base, seraph


def test_seccomp_profile_is_dockers_default_plus_exactly_the_bwrap_rules():
    profile, base, seraph = _rules()
    assert profile["defaultAction"] == "SCMP_ACT_ERRNO" and profile["defaultErrnoRet"] == 1
    assert sorted(name for rule in seraph for name in rule["names"]) == [
        "clone", "clone", "mount", "pivot_root", "umount2", "unshare"]
    clone = [rule for rule in seraph if rule["names"] == ["clone"]]
    assert {rule["args"][0]["index"] for rule in clone} == {0, 1}
    for rule in clone:
        assert rule["args"][0] | {"index": 0} == {"index": 0, "value": CLONE_NEWCGROUP, "valueTwo": 0,
                                                  "op": "SCMP_CMP_MASKED_EQ"}
    unshare = next(rule for rule in seraph if rule["names"] == ["unshare"])
    assert unshare["args"] == [{"index": 0, "value": CLONE_NEWCGROUP | CLONE_NEWTIME, "valueTwo": 0,
                                "op": "SCMP_CMP_MASKED_EQ"}]
    assert not any("includes" in rule and "caps" in rule["includes"] for rule in seraph)
    # Docker's own namespace rules are kept: clone without namespaces for
    # everyone, clone3 -> ENOSYS so glibc falls back to the filtered clone.
    assert any(rule["names"] == ["clone"] and rule.get("args", [{}])[0].get("value") == DOCKER_DEFAULT_NAMESPACE_MASK
               for rule in base)
    clone3 = next(rule for rule in base if rule["names"] == ["clone3"])
    assert clone3["action"] == "SCMP_ACT_ERRNO" and clone3["errnoRet"] == 38


def test_seccomp_profile_grants_nothing_broader_than_the_bwrap_needs():
    _profile, base, seraph = _rules()
    unconditional = {name for rule in base + seraph if rule["action"] == "SCMP_ACT_ALLOW"
                     and "caps" not in rule.get("includes", {}) and not rule.get("args")
                     for name in rule["names"]}
    for name in ("setns", "bpf", "open_tree", "move_mount", "fsopen", "fsmount", "fsconfig", "fspick",
                 "mount_setattr", "keyctl", "add_key", "request_key", "init_module", "finit_module", "kexec_load",
                 "reboot", "swapon", "chroot", "sethostname", "setdomainname", "clone", "clone3", "unshare",
                 "perf_event_open", "open_by_handle_at", "quotactl"):
        assert name not in unconditional, name
    assert unconditional & {"mount", "umount2", "pivot_root"} == {"mount", "umount2", "pivot_root"}


def test_seccomp_base_matches_a_supplied_docker_default(tmp_path):
    """With ``SERAPH_DOCKER_DEFAULT_SECCOMP`` set (CI), the base equals Docker's rules."""
    upstream = os.environ.get("SERAPH_DOCKER_DEFAULT_SECCOMP")
    sys.path.insert(0, str(REPO / "scripts"))
    try:
        import check_container_process_sandbox as script
    finally:
        sys.path.remove(str(REPO / "scripts"))
    profile, base, _seraph = _rules()
    # Self-consistency always: the base alone is a valid "upstream".
    (tmp_path / "base.json").write_text(json.dumps({**profile, "syscalls": base}))
    assert script.compare_seccomp(tmp_path / "base.json") == []
    if not upstream:
        pytest.skip("SERAPH_DOCKER_DEFAULT_SECCOMP is not set; the CI container job compares against Docker's file")
    assert script.compare_seccomp(Path(upstream)) == []


def test_apparmor_profile_is_docker_default_with_only_the_bwrap_relaxations():
    text = APPARMOR.read_text()
    rules = [line.split("#", 1)[0].strip() for line in text.splitlines()]
    rules = [rule for rule in rules if rule]
    assert "profile seraph-backend flags=(attach_disconnected,mediate_deleted) {" in rules
    assert {"userns,", "mount,", "pivot_root,", "umount,"} <= set(rules)
    assert not any(rule.startswith("deny mount") for rule in rules)
    for kept in ("deny @{PROC}/sysrq-trigger rwklx,", "deny @{PROC}/kcore rwklx,",
                 "deny /sys/kernel/security/** rwklx,", "deny /sys/firmware/** rwklx,"):
        assert kept in rules
    # Only user/max_user_namespaces is excepted from docker-default's /proc/sys write denial.
    assert "deny @{PROC}/sys/user/max_{cgroup,fanotify,inotify,ipc,mnt,net,pid,time,uts}_* w," in rules
    assert "deny @{PROC}/sys/{abi,crypto,debug,dev,fs,net,sunrpc,vm}/** w," in rules


def test_apparmor_profile_compiles_where_the_parser_is_available():
    parser = Path("/usr/sbin/apparmor_parser")
    if not parser.exists() or not Path("/etc/apparmor.d/abstractions/base").exists():
        pytest.skip("apparmor_parser is not installed on this host")
    completed = subprocess.run([str(parser), "-Q", "-K", "-I", "/etc/apparmor.d", str(APPARMOR)],
                               capture_output=True, text=True)
    if "userns" in completed.stderr and completed.returncode != 0:
        pytest.skip("AppArmor 3 parser without userns rules (documented: delete the userns line)")
    assert completed.returncode == 0, completed.stderr


def test_workspace_ownership_is_typed_for_non_root_users(tmp_path):
    (tmp_path / "file.txt").write_text("x")
    assert workspace_ownership_problem(tmp_path, uid=0) is None
    assert workspace_ownership_problem(tmp_path, uid=os.getuid()) is None
    assert workspace_ownership_problem(tmp_path, uid=os.getuid() + 1).startswith("workspace_ownership_mismatch:")
    assert workspace_ownership_problem(tmp_path / "missing", uid=os.getuid() + 1) is None


def test_container_preflight_refuses_a_foreign_owned_workspace_but_not_a_blocked_sandbox(tmp_path, monkeypatch):
    monkeypatch.setattr(process_isolation, "in_container", lambda: True)
    monkeypatch.setattr(process_isolation, "probe",
                        lambda *args, **kwargs: process_isolation.ProcessIsolationPosture(
                            "unavailable", reason="container_seccomp_profile_missing"))
    env = {"WORKSPACE_DIR": str(tmp_path), "SERAPH_AUTH_REQUIRED": "false"}
    report = production_preflight.build_preflight_report(env)
    checks = {check["name"]: check for check in report["core"]["checks"]}
    assert checks["process_sandbox"]["status"] == "blocked"
    assert "process_isolation_unavailable:container_seccomp_profile_missing" in checks["process_sandbox"]["detail"]
    assert checks["workspace_ownership"]["status"] == "ready"
    monkeypatch.setattr(production_preflight, "workspace_ownership_problem",
                        lambda _path: "workspace_ownership_mismatch:3_entries")
    report = production_preflight.build_preflight_report(env)
    checks = {check["name"]: check for check in report["core"]["checks"]}
    assert checks["workspace_ownership"]["status"] == "invalid"
    assert "chown -R 10001:10001 /app/data /app/logs" in checks["workspace_ownership"]["detail"]
    assert report["overall_status"] == "invalid"


def test_preflight_outside_a_container_does_not_probe(monkeypatch, tmp_path):
    monkeypatch.setattr(process_isolation, "in_container", lambda: False)
    monkeypatch.setattr(process_isolation, "probe", lambda *a, **k: pytest.fail("probed outside a container"))
    report = production_preflight.build_preflight_report({"WORKSPACE_DIR": str(tmp_path)})
    assert "process_sandbox" not in {check["name"] for check in report["core"]["checks"]}


def test_in_container_probe_inside_the_built_image():
    if os.environ.get("SERAPH_CONTAINER_SANDBOX_CHECK") != "1":
        pytest.skip("set SERAPH_CONTAINER_SANDBOX_CHECK=1 to build the image and probe inside it")
    if subprocess.run(["docker", "info"], capture_output=True).returncode != 0:
        pytest.skip("no reachable Docker daemon")
    completed = subprocess.run([sys.executable, str(REPO / "scripts" / "check_container_process_sandbox.py")],
                               capture_output=True, text=True, timeout=3600)
    assert completed.returncode == 0, completed.stdout + completed.stderr
