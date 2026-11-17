#!/usr/bin/env bash
# Live probe of one NEAR AI model with your own key (hidden prompt).
# Makes four tiny calls (well under one US cent in total) and reports what
# actually works, independent of what NEAR's catalog record claims:
#   1. plain chat completion (is the model served / ready?)
#   2. structured output with a JSON schema (response_format json_schema)
#   3. tool calling
#   4. TEE attestation report for the model
# The key is read from a hidden prompt and never written anywhere.
#
# Usage: ./scripts/near-model-probe.sh [model-id]
set -euo pipefail
MODEL="${1:-deepseek-ai/DeepSeek-V4.1-Flash-1017}"

python3 -I /dev/fd/3 "$MODEL" 3<<'PY' </dev/tty
import getpass, json, sys, time, urllib.error, urllib.parse, urllib.request

model = sys.argv[1]
base = "https://cloud-api.near.ai/v1"
key = getpass.getpass(f"NEAR AI API key for probing {model} (hidden): ").strip()
if not key:
    sys.exit("No key entered.")

def call(method, path, body=None, timeout=90):
    req = urllib.request.Request(base + path, method=method,
        data=None if body is None else json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    start = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read() or b"{}"), time.monotonic() - start
    except urllib.error.HTTPError as exc:
        raw = exc.read()[:600].decode("utf-8", "replace")
        return exc.code, {"error": raw}, time.monotonic() - start
    except Exception as exc:  # timeouts, DNS, TLS
        return None, {"error": repr(exc)}, time.monotonic() - start

results = {}

def report(name, ok, detail):
    results[name] = ok
    print(f"\n[{'PASS' if ok else 'FAIL'}] {name}\n  {detail}")

def chat(extra):
    return call("POST", "/chat/completions", {"model": model, "max_tokens": 400, **extra})

# 1. plain completion
status, body, secs = chat({"messages": [{"role": "user", "content": "Reply with exactly: pong"}]})
choice = (body.get("choices") or [{}])[0] if isinstance(body, dict) else {}
text = ((choice.get("message") or {}).get("content") or "").strip()
report("1. chat completion", status == 200 and bool(text),
       f"HTTP {status} in {secs:.1f}s, model echo={body.get('model')!r}, finish={choice.get('finish_reason')!r}, "
       f"reply={text[:80]!r}, usage={body.get('usage')}" if status == 200 else f"HTTP {status}: {body.get('error')}")

# 2. structured output (json_schema)
schema = {"type": "object", "properties": {"city": {"type": "string"}, "population_millions": {"type": "number"}},
          "required": ["city", "population_millions"], "additionalProperties": False}
status, body, secs = chat({
    "messages": [{"role": "user", "content": "Give the capital of Poland and its population in millions."}],
    "response_format": {"type": "json_schema", "json_schema": {"name": "capital", "strict": True, "schema": schema}}})
content = (((body.get("choices") or [{}])[0].get("message") or {}).get("content") or "") if status == 200 else ""
try:
    parsed = json.loads(content)
    valid = isinstance(parsed, dict) and set(parsed) == {"city", "population_millions"} \
        and isinstance(parsed["city"], str) and isinstance(parsed["population_millions"], (int, float))
except Exception:
    parsed, valid = None, False
report("2. structured output (json_schema)", status == 200 and valid,
       f"HTTP {status} in {secs:.1f}s, content={content[:160]!r}" if status == 200 else f"HTTP {status}: {body.get('error')}")

# 3. tool calling
tools = [{"type": "function", "function": {"name": "get_weather", "description": "Weather for a city",
          "parameters": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]}}}]
status, body, secs = chat({"messages": [{"role": "user", "content": "What's the weather in Warsaw? Use the tool."}],
                           "tools": tools, "tool_choice": "auto"})
calls = (((body.get("choices") or [{}])[0].get("message") or {}).get("tool_calls") or []) if status == 200 else []
names = [((c.get("function") or {}).get("name"), (c.get("function") or {}).get("arguments")) for c in calls]
report("3. tool calling", status == 200 and any(n == "get_weather" for n, _ in names),
       f"HTTP {status} in {secs:.1f}s, tool_calls={names}" if status == 200 else f"HTTP {status}: {body.get('error')}")

# 4. attestation report
q = urllib.parse.urlencode({"model": model})
status, body, secs = call("GET", f"/attestation/report?{q}", timeout=60)
if status == 200 and isinstance(body, dict):
    summary = {k: (v if isinstance(v, (str, int, float, bool)) and len(str(v)) < 80 else f"<{type(v).__name__}>")
               for k, v in list(body.items())[:10]}
    report("4. TEE attestation report", True, f"HTTP 200 in {secs:.1f}s, fields={summary}")
else:
    report("4. TEE attestation report", False, f"HTTP {status}: {str(body.get('error'))[:300]}")

del key
print("\nSummary:", ", ".join(f"{k.split('. ')[1]}={'yes' if v else 'no'}" for k, v in results.items()))
PY
