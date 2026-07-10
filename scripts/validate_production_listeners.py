#!/usr/bin/env python3
"""Report Docker-published listeners and reject private Seraph ports."""

from __future__ import annotations

import json
import re
import sys


forbidden = re.compile(r"(?:0\.0\.0\.0|\[::\]|:::|127\.0\.0\.1):(8000|8001|8004)->")
bad: list[dict[str, str]] = []
rows: list[dict[str, str]] = []
for line in sys.stdin:
    if not line.strip():
        continue
    row = json.loads(line)
    receipt = {"name": str(row.get("Names", "")), "ports": str(row.get("Ports", ""))}
    rows.append(receipt)
    if forbidden.search(receipt["ports"]):
        bad.append(receipt)
print(json.dumps({"container_listeners": rows, "unsafe_private_port_publications": bad}, sort_keys=True))
if bad:
    raise SystemExit("unsafe Docker publication of 8000, 8001, or 8004")
