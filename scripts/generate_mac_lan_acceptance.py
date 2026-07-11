#!/usr/bin/env python3
"""Run on the operator Mac; emits no credential or probe-key material."""
import argparse, hashlib, hmac, http.client, json, secrets, socket, ssl, time
from datetime import datetime, timezone
from pathlib import Path

p=argparse.ArgumentParser()
p.add_argument('--challenge-file',type=Path,required=True)
p.add_argument('--https-origin',required=True); p.add_argument('--lan-host',required=True); p.add_argument('--lan-ip',required=True)
p.add_argument('--trusted-ca',type=Path,required=True); p.add_argument('--operator-secret-file',type=Path,required=True)
p.add_argument('--probe-key-file',type=Path,required=True); p.add_argument('--client-identity',required=True)
a=p.parse_args(); origin=a.https_origin.rstrip('/'); challenge=json.loads(a.challenge_file.read_text()); challenge_raw=json.dumps(challenge,sort_keys=True,separators=(',',':')).encode()
from urllib.parse import urlsplit
parsed=urlsplit(origin); expected_host=parsed.hostname
if parsed.scheme!='https' or not expected_host: raise SystemExit('HTTPS origin required')
resolved={item[4][0] for item in socket.getaddrinfo(expected_host,parsed.port or 443,type=socket.SOCK_STREAM)}
if resolved!={a.lan_ip} or expected_host!=a.lan_host or challenge.get('expected_origin')!=origin or challenge.get('lan_host')!=a.lan_host or challenge.get('lan_ip')!=a.lan_ip or challenge.get('client_identity')!=a.client_identity: raise SystemExit('challenge/origin/host/IP/client mismatch')
password=a.operator_secret_file.read_text().strip(); key=a.probe_key_file.read_bytes().strip()
if not password or len(key)<32: raise SystemExit('required nonempty operator/probe key file missing')
ctx=ssl.create_default_context(cafile=str(a.trusted_ca)); port=parsed.port or 443
class PinnedHTTPSConnection(http.client.HTTPSConnection):
    def connect(self):
        raw=socket.create_connection((a.lan_ip,port),timeout=self.timeout)
        self.sock=ctx.wrap_socket(raw,server_hostname=expected_host)
        if self.sock.getpeername()[0]!=a.lan_ip:
            self.sock.close(); raise OSError('TLS peer IP mismatch')
def request(method,path,body=None,headers=None):
    connection=PinnedHTTPSConnection(expected_host,port=port,timeout=10,context=ctx)
    request_headers={'Host':parsed.netloc,**(headers or {})}
    connection.request(method,path,body=body,headers=request_headers)
    response=connection.getresponse(); payload=response.read(); response_headers=response.getheaders(); connection.close()
    if response.status < 200 or response.status >= 300: raise SystemExit('authenticated HTTPS probe failed')
    return json.loads(payload),response_headers
login_payload,login_headers=request('POST','/api/auth/login',json.dumps({'password':password}).encode(),{'Content-Type':'application/json'})
cookies=[value.split(';',1)[0] for name,value in login_headers if name.lower()=='set-cookie']
session_payload,_=request('GET','/api/auth/session',headers={'Cookie':'; '.join(cookies)})
authenticated=login_payload.get('authenticated') is True and session_payload.get('authenticated') is True
probes={}
for port in (8000,8001,8004):
    started=time.monotonic(); connected=False; error=''
    try:
        with socket.create_connection((a.lan_ip,port),timeout=3): connected=True
    except ConnectionRefusedError: error='connection_refused'
    except TimeoutError: error='timeout'
    except Exception: error='invalid_network_error'
    probes[str(port)]={'connected':connected,'error_class':error,'latency_ms':round((time.monotonic()-started)*1000)}
receipt={'schema':'seraph.mac-lan-acceptance.v1','challenge':challenge,'challenge_sha256':hashlib.sha256(challenge_raw).hexdigest(),'https_origin':origin,'lan_host':a.lan_host,'lan_ip':a.lan_ip,'client_identity':a.client_identity,'captured_at':datetime.now(timezone.utc).isoformat(),'nonce':secrets.token_hex(24),'authenticated_session':authenticated,'port_probes':probes}
canonical=json.dumps(receipt,sort_keys=True,separators=(',',':')).encode(); receipt['hmac_sha256']=hmac.new(key,canonical,hashlib.sha256).hexdigest()
print(json.dumps(receipt,indent=2,sort_keys=True))
