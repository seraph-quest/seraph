#!/usr/bin/env python3
import hashlib, hmac, json, os, re, sys
from datetime import datetime, timezone

r=json.load(sys.stdin); historical=os.environ.get('SERAPH_MAC_RECEIPT_HISTORICAL')=='true'
def fail(x): raise SystemExit('Mac operator acceptance invalid: '+x)
expected_origin=os.environ.get('SERAPH_EXPECTED_HTTPS_ORIGIN','').rstrip('/')
expected_host=os.environ.get('SERAPH_LAN_HOST','')
expected_ip=os.environ.get('SERAPH_LAN_IP','')
expected_client=os.environ.get('SERAPH_MAC_PROBE_CLIENT_ID','')
key_file=os.environ.get('SERAPH_MAC_PROBE_KEY_FILE',''); challenge_file=os.environ.get('SERAPH_ACCEPTANCE_CHALLENGE_FILE','')
if not all((expected_origin,expected_host,expected_ip,expected_client,key_file,challenge_file)): fail('server expectations are incomplete')
if r.get('schema')!='seraph.mac-lan-acceptance.v1': fail('schema mismatch')
if r.get('https_origin')!=expected_origin or r.get('lan_host')!=expected_host or r.get('lan_ip')!=expected_ip: fail('origin/host/IP mismatch')
if r.get('client_identity')!=expected_client: fail('client identity mismatch')
if not re.fullmatch(r'[0-9a-f]{32,128}',str(r.get('nonce',''))): fail('nonce invalid')
try: age=(datetime.now(timezone.utc)-datetime.fromisoformat(r['captured_at'].replace('Z','+00:00'))).total_seconds()
except Exception: fail('invalid captured_at')
if not historical and (age < -30 or age > 300): fail('stale receipt')
if r.get('authenticated_session') is not True: fail('authenticated session proof missing')
for port in ('8000','8001','8004'):
    probe=r.get('port_probes',{}).get(port,{})
    if probe.get('connected') is not False or probe.get('error_class') not in {'connection_refused','timeout'}: fail(f'measured LAN denial missing for {port}')
expected_challenge=json.load(open(challenge_file)); canonical_challenge=json.dumps(expected_challenge,sort_keys=True,separators=(',',':')).encode()
if r.get('challenge')!=expected_challenge or r.get('challenge_sha256')!=hashlib.sha256(canonical_challenge).hexdigest(): fail('acceptance challenge mismatch')
signature=str(r.pop('hmac_sha256',''))
canonical=json.dumps(r,sort_keys=True,separators=(',',':')).encode()
key=open(key_file,'rb').read().strip()
if len(key)<32: fail('probe key is missing/too short')
if not hmac.compare_digest(signature,hmac.new(key,canonical,hashlib.sha256).hexdigest()): fail('HMAC mismatch')
print('Mac operator acceptance valid: authenticated origin and measured internal-port denials verified')
