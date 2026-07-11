#!/usr/bin/env python3
"""Generate the local identity/listener gate used before candidate startup."""
import hashlib, json, socket, subprocess
from datetime import datetime, timezone
from pathlib import Path

machine = next((p for p in (Path('/etc/machine-id'), Path('/var/lib/dbus/machine-id')) if p.is_file()), None)
if not machine:
    raise SystemExit('machine-id unavailable')
ss = subprocess.run(['ss', '-lntp'], text=True, capture_output=True, check=True).stdout
network = subprocess.run(['docker', 'network', 'inspect', 'bridge'], text=True, capture_output=True, check=True)
gateway = str(json.loads(network.stdout)[0].get('IPAM', {}).get('Config', [{}])[0].get('Gateway', ''))
print(json.dumps({'local_hostname': socket.gethostname(), 'machine_identity_sha256': hashlib.sha256(machine.read_bytes().strip()).hexdigest(), 'captured_at': datetime.now(timezone.utc).isoformat(), 'ss_lntp': ss, 'docker_bridge_addresses': [gateway] if gateway else [], 'docker_network_bindings': {'host-gateway': gateway} if gateway else {}}, indent=2, sort_keys=True))
