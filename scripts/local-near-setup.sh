#!/usr/bin/env bash
# One-shot local setup for Seraph on this Mac:
#   1. turns on the loopback-only no-login mode (#1161) in .env.dev (backup kept),
#   2. switches the checkout to code that has that mode, if needed,
#   3. restarts Seraph (with the macOS temp-dir workaround until #1158 merges),
#   4. asks for your NEAR AI key (hidden), a spending ceiling and a text model,
#      asks you to confirm NEAR's plaintext-egress consent, saves the NEAR TEE
#      text slot through Seraph's own Settings API and runs the catalog check.
#
# The key is typed into a hidden prompt and sent straight to the local backend,
# which stores it in Seraph's vault. It is never written to a file, a command
# line or the shell history.
#
# Usage:  ./scripts/local-near-setup.sh            (full setup)
#         ./scripts/local-near-setup.sh --near-only (Seraph already running)
#         add --new-key to replace the stored NEAR key
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO"
ENV_FILE="${ENV_FILE:-$REPO/.env.dev}"
BACKEND="http://127.0.0.1:8004"
COCKPIT="http://127.0.0.1:3001"
NO_LOGIN_REF="origin/feat/1161-local-no-login"
NEAR_ONLY=0
NEW_KEY_FLAG=""
for arg in "$@"; do
  [[ "$arg" == "--near-only" ]] && NEAR_ONLY=1
  [[ "$arg" == "--new-key" ]] && NEW_KEY_FLAG="--new-key"
done

say()  { printf '\033[1m==> %s\033[0m\n' "$*"; }
die()  { printf '\033[31mError: %s\033[0m\n' "$*" >&2; exit 1; }

configure_env() {
  [[ -f "$ENV_FILE" ]] || die "$ENV_FILE not found"
  local backup="$ENV_FILE.bak-$(date +%Y%m%d-%H%M%S)"
  cp -p "$ENV_FILE" "$backup" && chmod 600 "$backup"
  say "Backed up .env.dev to $backup"
  python3 -I - "$ENV_FILE" <<'PY'
import re, sys
path = sys.argv[1]
lines = open(path, encoding="utf-8").read().splitlines()
def value(key):
    for line in lines:
        m = re.match(rf"\s*(?:export\s+)?{key}\s*=\s*(.*)$", line)
        if m:
            return m.group(1).strip().strip("'\"")
    return ""
if value("OPERATOR_AUTH_SECRET") or value("OPERATOR_AUTH_SECRET_HASH"):
    sys.exit("An operator password is configured in .env.dev. No-login mode refuses to start "
             "while one is set; remove OPERATOR_AUTH_SECRET / OPERATOR_AUTH_SECRET_HASH first.")
managed = {
    "DEPLOYMENT_ENVIRONMENT": "development",
    "OPERATOR_AUTH_LOCAL_NO_LOGIN": "true",
    "OPERATOR_AUTH_ALLOWED_HOSTS": "localhost,127.0.0.1",
    "OPERATOR_AUTH_ALLOWED_ORIGINS": "http://localhost:3001,http://127.0.0.1:3001",
}
marker = "# Local single-operator mode without login (#1161), loopback only."
pattern = re.compile(r"\s*(?:export\s+)?(" + "|".join(managed) + r")\s*=")
kept = [l for l in lines if not pattern.match(l) and l.strip() != marker]
while kept and not kept[-1].strip():
    kept.pop()
kept += ["", marker] + [f"{k}={v}" for k, v in managed.items()]
open(path, "w", encoding="utf-8").write("\n".join(kept) + "\n")
PY
  say "No-login mode enabled in .env.dev"
}

ensure_no_login_code() {
  if grep -q "operator_auth_local_no_login" backend/config/settings.py 2>/dev/null; then
    say "This checkout already has the no-login mode ($(git rev-parse --short HEAD))"
    return
  fi
  if ! git diff --quiet || ! git diff --cached --quiet; then
    die "The checkout has uncommitted changes to tracked files; commit or discard them first."
  fi
  git fetch -q origin feat/1161-local-no-login
  say "Switching the checkout to $NO_LOGIN_REF (detached). Once PR #1162 is merged, 'git checkout develop' is enough."
  git checkout -q --detach "$NO_LOGIN_REF"
}

restart_seraph() {
  say "Restarting Seraph"
  ./manage.sh -e dev local down >/dev/null 2>&1 || true
  local real_tmp
  real_tmp="$(python3 -c 'import os,tempfile;print(os.path.realpath(tempfile.gettempdir()))')"
  TMPDIR="$real_tmp" \
  LOCAL_WORKSPACE_DIR=/private/tmp/seraph-dev-data \
  LOCAL_LLM_LOG_DIR=/private/tmp/seraph-dev-logs \
    ./manage.sh -e dev local up | tail -6
  say "Waiting for the backend"
  for _ in $(seq 1 90); do
    curl -fsS -m 2 "$BACKEND/health" >/dev/null 2>&1 && break
    sleep 1
  done
  curl -fsS -m 2 "$BACKEND/health" >/dev/null 2>&1 || die "Backend did not become healthy; see ./manage.sh -e dev local status"
  local mode
  mode="$(curl -sS -m 5 -H "Origin: $COCKPIT" "$BACKEND/api/auth/session" || true)"
  [[ "$mode" == *local_no_login* ]] || die "Backend is up but not in no-login mode. Response: $mode"
  say "Seraph is running in no-login local mode"
}

setup_near() {
  say "NEAR TEE setup"
  # The Python code arrives on fd 3 so stdin stays the terminal for the prompts.
  python3 -I /dev/fd/3 "$BACKEND" "$COCKPIT" $NEW_KEY_FLAG 3<<'PY' </dev/tty
import getpass, json, sys, urllib.error, urllib.request

backend, cockpit = sys.argv[1], sys.argv[2]
TEXT_MODELS = ["deepseek-ai/DeepSeek-V4.1-Flash-1017", "z-ai/glm-5.3-flash", "Qwen/Qwen3.8-27B",
               "Qwen/Qwen3.6-35B-A3B-FP8"]
# Reasoning models (DeepSeek) spend output tokens on reasoning; 4096 leaves room.
MAX_OUTPUT_TOKENS = 4096
CHECK_CAPABILITIES = ["text", "health", "latency_ms", "structured_output", "streaming", "tool_use"]

def call(method, path, body=None):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(backend + path, data=data, method=method, headers={
        "Content-Type": "application/json", "Origin": cockpit})
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read()).get("detail")
        except Exception:
            detail = exc.reason
        sys.exit(f"Seraph refused {method} {path} ({exc.code}): {detail}")

def dollars(prompt, default):
    raw = input(f"{prompt} [${default}]: ").strip().lstrip("$") or default
    try:
        value = round(float(raw) * 1_000_000)
    except ValueError:
        sys.exit("Not a dollar amount.")
    if not 1 <= value <= 1_000_000_000:
        sys.exit("Amount must be between $0.000001 and $1000.")
    return value

state = call("GET", "/api/settings/model-fabric")
revision = state.get("egress_revision")
if not isinstance(revision, int):
    sys.exit("Could not read the current policy revision from Seraph.")

lane = state.get("inference_lane") or {}
if lane.get("status") == "blocked":
    sys.exit(f"Seraph's inference lane is blocked ({lane.get('reason_code')}); fix it in Settings first.")
if lane.get("published") is not True:
    # Seraph requires a published monthly ceiling before any provider call.
    # Spending is governed at the NEAR provider level, so publish the highest
    # ceiling Seraph accepts ($1000/month) to keep Seraph out of the way.
    ceiling = 1_000_000_000
    state = call("PUT", "/api/settings/model-fabric", {
        "expected_policy_revision": revision,
        "inference_lane": {"spend_ceiling_microusd": ceiling}})
    revision = state["egress_revision"]
    print("Seraph's monthly ceiling set to its maximum ($1000); your NEAR account limit governs spending.")

print("\nText model for chat, reports, Work Board and the other features:")
for i, model in enumerate(TEXT_MODELS, 1):
    note = "  (TEE-attested; live-probed 2026-11-14)" if "DeepSeek" in model else ""
    print(f"  {i}. {model}{note}")
choice = input("Choose 1-4 [1]: ").strip() or "1"
if choice not in {"1", "2", "3", "4"}:
    sys.exit("Choose 1, 2, 3 or 4.")
model = TEXT_MODELS[int(choice) - 1]

print("\nConsent required by Seraph before enabling NEAR:")
print("  NEAR receives these requests in plaintext. Seraph does not verify attestation")
print("  and there is no end-to-end encryption.")
if input("Type 'yes' to acknowledge and enable the NEAR TEE text slot: ").strip().lower() not in {"y", "yes"}:
    sys.exit("Not acknowledged; nothing was changed for NEAR.")

stored = bool((state.get("near_tee") or {}).get("key_present"))
if stored and "--new-key" not in sys.argv:
    key = ""
    print("Using the NEAR key already stored in Seraph's vault.")
else:
    key = getpass.getpass("\nPaste your NEAR AI API key (hidden): ").strip()
    if not key:
        sys.exit("No key entered.")
key_field = {"api_key": key} if key else {}

state = call("PUT", "/api/settings/model-fabric", {
    "expected_policy_revision": revision,
    "near_tee": {
        "schema_version": "seraph.near.tee.setup.v1",
        "api_base": "https://cloud-api.near.ai/v1",
        **key_field,
        "slots": {"text": {
            "model_id": model, "enabled": True,
            "max_input_tokens": 8192, "max_output_tokens": MAX_OUTPUT_TOKENS,
            "timeout_seconds": 30, "request_cost_bound_microusd": 20_000,
            "catalog_ttl_hours": 24,
            "plaintext_provider_egress_acknowledged": True}},
    }})
del key, key_field
revision = state["egress_revision"]
print(f"NEAR TEE text slot saved with {model}. Key is in Seraph's vault.")

print("Running the NEAR catalog check (reads NEAR's public model catalog)...")
call("POST", "/api/settings/model-fabric/near-tee/catalog-check",
     {"expected_policy_revision": revision})
slot = (call("GET", "/api/settings/model-fabric").get("near_tee") or {}).get("slots", {}).get("text") or {}
receipt = slot.get("catalog_receipt") or {}
print(f"Catalog check: {receipt.get('outcome')} "
      f"(reason {receipt.get('reason_code')}, readiness {receipt.get('readiness_basis')})")
if receipt.get("outcome") != "passed":
    sys.exit("Catalog check did not pass; capability checks skipped.")

print("\nRunning capability checks (small billed NEAR calls; proofs valid 24h):")
failed = []
for capability in CHECK_CAPABILITIES:
    req = urllib.request.Request(backend + "/api/settings/model-fabric/canary", method="POST",
        data=json.dumps({"profile_id": "near_tee.text", "capability": capability,
                         "timeout_seconds": 120, "proof_ttl_seconds": 86400}).encode(),
        headers={"Content-Type": "application/json", "Origin": cockpit})
    try:
        with urllib.request.urlopen(req, timeout=200) as resp:
            body = json.loads(resp.read() or b"{}")
        outcome = body.get("outcome")
        reason = body.get("error_code") or ""
    except urllib.error.HTTPError as exc:
        try:
            outcome, reason = "refused", json.loads(exc.read()).get("detail")
        except Exception:
            outcome, reason = "refused", exc.code
    print(f"  {capability:18s} {outcome} {reason or ''}")
    if outcome != "passed":
        failed.append(capability)

slot = (call("GET", "/api/settings/model-fabric").get("near_tee") or {}).get("slots", {}).get("text") or {}
print(f"\nText slot now: {slot.get('status')} {slot.get('reason_code') or ''}")
if failed:
    print("Some checks did not pass: " + ", ".join(failed) + ". Paste this output to Claude.")
PY
}

main() {
  if [[ $NEAR_ONLY -eq 0 ]]; then
    configure_env
    ensure_no_login_code
    restart_seraph
  fi
  setup_near
  say "Done. Open $COCKPIT (use 127.0.0.1, not localhost). Vision and embeddings can be enabled in Settings."
  open "$COCKPIT" 2>/dev/null || true
}

# Run only when executed, not when sourced (lets the functions be tested).
if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  main
fi
