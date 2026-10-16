"""Run the actual Forgejo form tests against a disposable local Forgejo 15.0.9.

Usage (from backend/):
    FORGEJO_TEST_BINARY=/path/to/forgejo-15.0.9-linux-amd64 \\
        .venv/bin/python scripts/run_forgejo_form_fixture.py [--keep] [pytest args...]

The pinned, signature-verified Forgejo binary serves the logical site
``https://codeberg.org`` on 127.0.0.1 only (offline mode, no mail, webhooks,
federation or registration). A fresh private root holds its database,
repositories, the disposable fixture credentials and the Seraph workspace; the
whole provider process group is killed and reaped on exit. The root (which
holds the disposable credentials, database and log) is deleted after a passing
run unless --keep is given; a failing run keeps it for inspection. The binary
is hashed once and the verified bytes are copied into the private root and
executed from there, so nothing can swap it between the check and the exec. Tests reach it only
through their loopback transport and launch Playwright's Chromium with its own
sandbox enabled. No network beyond localhost and no inference is used.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time

import httpx

BINARY_SHA256 = "04bf4981241cd7a39554809686d3e1811e219073cbf9a6ce6ed3895adfd8d17a"
VERSION_PREFIX = b"forgejo version 15.0.9+gitea-1.22.0 (release name 15.0.9) "
# fixture1013 serves the form tests; FORGEJO_FIXTURE_NAME=fixture925 serves
# the existing issue-title test (tests/test_forgejo_native_actual.py).
REPOSITORY = os.environ.get("FORGEJO_FIXTURE_NAME", "fixture1013")
if REPOSITORY not in ("fixture1013", "fixture925"):
    raise SystemExit("FORGEJO_FIXTURE_NAME must be fixture1013 or fixture925")
USER = "seraph_" + REPOSITORY
APP_INI = """APP_NAME = Seraph disposable Forgejo form fixture
RUN_MODE = prod
WORK_PATH = {root}
[server]
PROTOCOL = http
DOMAIN = codeberg.org
HTTP_ADDR = 127.0.0.1
HTTP_PORT = {port}
ROOT_URL = https://codeberg.org/
DISABLE_SSH = true
OFFLINE_MODE = true
[database]
DB_TYPE = sqlite3
PATH = {root}/forgejo.db
[repository]
ROOT = {root}/repositories
[security]
INSTALL_LOCK = true
DISABLE_WEBHOOKS = true
DISABLE_GIT_HOOKS = true
IMPORT_LOCAL_PATHS = false
[service]
DISABLE_REGISTRATION = true
ENABLE_NOTIFY_MAIL = false
ENABLE_CAPTCHA = false
REQUIRE_SIGNIN_VIEW = true
[mailer]
ENABLED = false
[openid]
ENABLE_OPENID_SIGNIN = false
ENABLE_OPENID_SIGNUP = false
[oauth2]
ENABLED = false
[actions]
ENABLED = false
[packages]
ENABLED = false
[federation]
ENABLED = false
[picture]
DISABLE_GRAVATAR = true
ENABLE_FEDERATED_AVATAR = false
[cron]
ENABLED = false
[log]
MODE = console
LEVEL = Warn
[session]
COOKIE_SECURE = true
SESSION_LIFE_TIME = 900
"""


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _close_group(process: subprocess.Popen) -> None:
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            break
        try:
            process.wait(timeout=10)
            break
        except subprocess.TimeoutExpired:
            continue
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def main(pytest_args: list[str]) -> int:
    keep = "--keep" in pytest_args
    pytest_args = [arg for arg in pytest_args if arg != "--keep"]
    source = Path(os.environ.get("FORGEJO_TEST_BINARY", "")).expanduser()
    verified = source.read_bytes() if source.is_file() else b""
    if hashlib.sha256(verified).hexdigest() != BINARY_SHA256:
        print("FORGEJO_TEST_BINARY must be the pinned Forgejo 15.0.9 binary", file=sys.stderr)
        return 2
    root = Path(tempfile.mkdtemp(prefix="forgejo-form-fixture-"))
    os.chmod(root, 0o700)
    for name in ("home", "tmp", "seraph-workspace", "bin"):
        (root / name).mkdir(mode=0o700)
    binary = root / "bin" / "forgejo"
    descriptor = os.open(binary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o700)
    try:
        os.write(descriptor, verified)
    finally:
        os.close(descriptor)
    passed = False
    try:
        result = _run(root, binary, pytest_args)
        passed = result == 0
        return result
    finally:
        if passed and not keep:
            shutil.rmtree(root, ignore_errors=True)
        else:
            print("fixture root kept (disposable credentials inside): " + str(root), file=sys.stderr)


def _run(root: Path, binary: Path, pytest_args: list[str]) -> int:
    env = {"PATH": "/usr/bin:/bin", "HOME": str(root / "home"), "TMPDIR": str(root / "tmp"), "LANG": "C.UTF-8",
           "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": str(root / "home/.gitconfig")}
    version = subprocess.run([str(binary), "--version"], env=env, capture_output=True, timeout=30, check=True).stdout
    if not version.startswith(VERSION_PREFIX):
        print("unexpected Forgejo version: " + version.decode(errors="replace"), file=sys.stderr)
        return 2
    port = _free_port()
    (root / "app.ini").write_text(APP_INI.format(root=root, port=port))
    common = [str(binary), "--work-path", str(root), "--config", str(root / "app.ini")]
    credentials = {"user_name": USER, "password": secrets.token_urlsafe(32)}
    credential_file = root / "credentials.json"
    credential_file.write_text(json.dumps(credentials))
    os.chmod(credential_file, 0o600)
    log = (root / "provider.log").open("wb")
    provider = subprocess.Popen(common + ["web"], env=env, cwd=root, start_new_session=True, stdout=log, stderr=log)
    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", headers={"Host": "codeberg.org"},
                          trust_env=False, follow_redirects=False, timeout=5) as client:
            deadline = time.monotonic() + 60
            while True:
                try:
                    if client.get("/").status_code in (200, 303):
                        break
                except httpx.TransportError:
                    pass
                if provider.poll() is not None or time.monotonic() > deadline:
                    print("local Forgejo did not become ready; see " + str(root / "provider.log"), file=sys.stderr)
                    return 2
                time.sleep(0.2)
            subprocess.run(common + ["admin", "user", "create", "--username", USER, "--password",
                credentials["password"], "--email", REPOSITORY + "@example.invalid", "--must-change-password=false"],
                env=env, cwd=root, capture_output=True, timeout=60, check=True)
            client.auth = (USER, credentials["password"])
            actor = client.get("/api/v1/user")
            repo = client.post("/api/v1/user/repos", json={"name": REPOSITORY, "private": True, "auto_init": False})
            issue = client.post(f"/api/v1/repos/{USER}/{REPOSITORY}/issues",
                                json={"title": "Ordinary fixture issue", "body": "Owned disposable setup only"})
            if (actor.status_code, repo.status_code, issue.status_code) != (200, 201, 201):
                print("fixture bootstrap failed", file=sys.stderr)
                return 2
            (root / "bootstrap.json").write_text(json.dumps({"actor": actor.json(), "repository": repo.json(),
                "issue": issue.json(), "dependency_bootstrap_only": True}))
        test_env = {**os.environ, "FORGEJO_TEST_CREDENTIAL_FILE": str(credential_file),
                    "FORGEJO_TEST_TCP_PORT": str(port), "WORKSPACE_DIR": str(root / "seraph-workspace"),
                    "DATABASE_URL": "sqlite+aiosqlite:///" + str(root / "seraph-workspace/isolated.sqlite")}
        for name in list(test_env):
            if any(marker in name.upper() for marker in ("API_KEY", "TOKEN", "SECRET", "PASSWORD")):
                test_env.pop(name)
        if not any(arg.startswith("tests/") for arg in pytest_args):
            pytest_args = [*pytest_args, "tests/test_forgejo_forms_actual.py",
                           "tests/test_forgejo_form_journeys_actual.py", "tests/test_forgejo_forms_ui_actual.py"]
        test = subprocess.Popen([sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "--no-cov", *pytest_args],
            env=test_env, start_new_session=True)
        try:
            return test.wait(timeout=900)
        finally:
            _close_group(test)
    finally:
        _close_group(provider)
        log.close()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
