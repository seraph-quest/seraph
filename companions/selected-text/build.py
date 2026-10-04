"""Materialize one reviewed exact core-origin profile; no installer or network."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
from urllib.parse import urlsplit

parser=argparse.ArgumentParser()
parser.add_argument("--core-origin",required=True)
parser.add_argument("--output",type=Path,required=True)
args=parser.parse_args()
origin=urlsplit(args.core_origin)
if origin.scheme not in {"https","http"} or not origin.hostname or origin.username or origin.password or origin.path or origin.query or origin.fragment or (origin.scheme=="http" and origin.hostname not in {"localhost","127.0.0.1","::1"}):
    parser.error("one exact HTTPS origin or same-host HTTP loopback origin required")
origin.port
root=Path(__file__).resolve().parent
names=["worker.js","protocol.js","preview.js","preview.html"]
build=hashlib.sha256(b"".join(name.encode()+b"\0"+(root/name).read_bytes()+b"\0" for name in names)).hexdigest()
declared=re.search(r'ADAPTER_BUILD_DIGEST = "([a-f0-9]{64})"',(root.parents[1]/"backend/src/workflows/selected_context_contract.py").read_text()).group(1)
if declared!=build:
    parser.error("source build differs from the reviewed server profile; update requires source review")
args.output.mkdir(mode=0o700,parents=True,exist_ok=False)
for name in names:shutil.copyfile(root/name,args.output/name)
manifest=json.loads((root/"manifest.json").read_text())
manifest["host_permissions"]=[args.core_origin+"/*"]
(args.output/"manifest.json").write_text(json.dumps(manifest,indent=2)+"\n")
(args.output/"core.js").write_text("const SERAPH_CORE_ORIGIN = "+json.dumps(args.core_origin)+";\nconst SERAPH_ADAPTER_BUILD_DIGEST = "+json.dumps(build)+";\n")
print(json.dumps({"core_origin":args.core_origin,"adapter_build_digest":build,"permissions":manifest["permissions"],"host_permissions":manifest["host_permissions"],"publisher_trust_claim":False}))
