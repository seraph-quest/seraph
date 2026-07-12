#!/usr/bin/env python3
import json
import sys
from collections import Counter

raw = sys.stdin.read().strip()
if not raw:
    raise SystemExit("compose state is empty")
try:
    parsed = json.loads(raw)
    rows = parsed if isinstance(parsed, list) else [parsed]
except json.JSONDecodeError:
    rows = [json.loads(line) for line in raw.splitlines() if line.strip()]
expected = {"backend", "ingress", "vlm-wrapper", "gpu-model"}
counts = Counter(str(row.get("Service", "")) for row in rows)
expected_counts = Counter({service: 1 for service in expected})
if len(rows) != 4 or counts != expected_counts:
    raise SystemExit(f"expected exactly one row per service {sorted(expected)}, found {dict(counts)}")
for row in rows:
    if str(row.get("State", "")).lower() != "running":
        raise SystemExit(f"service {row.get('Service')} is not running")
    if str(row.get("Health", "")).lower() != "healthy":
        raise SystemExit(f"service {row.get('Service')} is not healthy")
print("compose runtime valid: ingress, backend, VLM wrapper, and GPU model are running and healthy")
