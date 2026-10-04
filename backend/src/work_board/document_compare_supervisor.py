"""Trusted witness-only supervisor; parser owns no canonical writes.

The private directory descriptor comes from the trusted parent, never typed
input. A positive wait receipt survives a parent crash; absent proof is Unknown.
"""
from __future__ import annotations
import json
import math
import os
from pathlib import Path
import signal
import stat
import struct
import subprocess
import sys
import time

MAX_OUTPUT = 512 * 1024


def main():
    if len(sys.argv) != 4: return 2
    directory = int(sys.argv[1]); binding = json.loads(sys.argv[2])
    absolute_deadline=float(sys.argv[3])
    allowance=absolute_deadline-time.time()
    if not math.isfinite(absolute_deadline) or not 5<allowance<=70:return 2
    wait_deadline=time.monotonic()+min(35,allowance)
    if (set(binding) != {"job_id", "input_digest", "generation", "nonce"}
        or len(binding["nonce"]) != 32 or any(c not in "0123456789abcdef" for c in binding["nonce"])
        or len(binding["input_digest"]) != 64 or binding["generation"] not in (1,2)):
        return 2
    metadata = os.fstat(directory)
    if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.getuid() or metadata.st_mode & 0o077:
        return 2
    # Kill the actual parser with five seconds left for positive wait/reap.
    # Parent death cannot leave a parser running beyond this original window.
    def expire(_signal,_frame):raise TimeoutError("document_supervisor_deadline")
    signal.signal(signal.SIGALRM,expire)
    signal.setitimer(signal.ITIMER_REAL,max(.001,wait_deadline-time.monotonic()-5))
    parser = subprocess.Popen([sys.executable, "-I", str(Path(__file__).with_name("document_compare_child.py")), binding["nonce"]],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, close_fds=True)
    result=b""; reason=None
    try:
        ready=parser.stdout.readline(4097)
        packet=json.loads(ready)
        if len(ready)>4096 or packet.get("state")!="ready" or packet.get("nonce")!=binding["nonce"]:
            reason="document_resource_self_check_failed"
        else:
            packet.update({"supervisor_pid":os.getpid(),"parser_pid":parser.pid,"binding":binding})
            print(json.dumps(packet),flush=True)
            header=sys.stdin.buffer.read(8)
            if len(header)!=8: raise ValueError("document_parent_source_incomplete")
            a,b=struct.unpack("!II",header)
            if not 1<=a<=2*1024*1024 or not 1<=b<=1024*1024: raise ValueError("document_parent_source_bound")
            parser.stdin.write(header)
            remaining=a+b
            while remaining:
                chunk=sys.stdin.buffer.read(min(65536,remaining))
                if not chunk: raise ValueError("document_parent_source_incomplete")
                parser.stdin.write(chunk); remaining-=len(chunk)
            if sys.stdin.buffer.read(1): raise ValueError("document_parent_source_extra")
            parser.stdin.close()
            result=parser.stdout.read(MAX_OUTPUT+1)
            if len(result)>MAX_OUTPUT: raise ValueError("document_output_pipe_bound")
    except TimeoutError:
        reason="document_supervisor_deadline"
    except (ValueError, OSError, json.JSONDecodeError):
        reason="document_supervisor_interrupted"
    finally:
        signal.setitimer(signal.ITIMER_REAL,0)
        if reason is not None and parser.poll() is None:
            parser.kill()
        try:
            exit_code=parser.wait(timeout=max(0,min(5,wait_deadline-time.monotonic())))
        except subprocess.TimeoutExpired:
            parser.kill();exit_code=parser.wait(timeout=max(0,min(5,wait_deadline-time.monotonic())));reason="document_parser_quiescence_timeout"
        witness={**binding,"supervisor_pid":os.getpid(),"parser_pid":parser.pid,
            "parser_exit":exit_code,"wait_reaped":True,"reason":reason}
        raw=json.dumps(witness,sort_keys=True,separators=(",",":")).encode()
        fd=os.open(binding["nonce"]+".witness.json",os.O_WRONLY|os.O_CREAT|os.O_EXCL|getattr(os,"O_NOFOLLOW",0),0o600,dir_fd=directory)
        try:
            os.write(fd,raw); os.fsync(fd)
        finally: os.close(fd)
        os.fsync(directory)
    if reason or exit_code!=0:
        result=json.dumps({"status":"blocked","reason":reason or "document_parser_resource_exit","no_learning":True}).encode()
    try:
        sys.stdout.buffer.write(result); sys.stdout.buffer.flush()
    except BrokenPipeError:
        pass  # The persisted actual-reap witness is authoritative.
    return 0


if __name__ == "__main__": raise SystemExit(main())
