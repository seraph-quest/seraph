#!/usr/bin/env python3
import json, os, re, sys
from datetime import datetime, timezone

r=json.load(sys.stdin)
def fail(x): raise SystemExit('GPU predeploy invalid: '+x)
if r.get('local_hostname') != os.environ.get('SERAPH_GPU_EXPECTED_HOSTNAME'): fail('hostname mismatch')
if r.get('machine_identity_sha256') != os.environ.get('SERAPH_GPU_MACHINE_IDENTITY_SHA256'): fail('machine identity mismatch')
try: age=(datetime.now(timezone.utc)-datetime.fromisoformat(r['captured_at'].replace('Z','+00:00'))).total_seconds()
except Exception: fail('invalid captured_at')
if age < -30 or age > int(os.environ.get('SERAPH_HOST_INVENTORY_MAX_AGE_SECONDS','900')): fail('stale receipt')
allowed={'127.0.0.1','::1','[::1]'} | set(r.get('docker_bridge_addresses',[]))
for line in str(r.get('ss_lntp','')).splitlines():
    m=re.search(r'LISTEN\s+\d+\s+\d+\s+(\S+):(8000|8001|8004)\b',line)
    if not m: continue
    address,port=m.groups()
    if port in {'8001','8004'}: fail(f'old service listener {address}:{port} must be stopped before managed cutover')
    if address not in allowed: fail(f'model listener is LAN/wildcard: {address}:{port}')
if r.get('docker_network_bindings',{}).get('host-gateway') not in allowed: fail('host-gateway binding missing')
print('GPU predeploy valid: local identity and private model listener verified; old wrapper/backend stopped')
