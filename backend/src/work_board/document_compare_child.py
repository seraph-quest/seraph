"""Fixed trusted resource-limited child; stdin/out are anonymous bounded pipes."""
from __future__ import annotations

import errno
import importlib.util
import json
import mmap
from pathlib import Path
import signal
import struct
import sys

MAX_PAIR = 3 * 1024 * 1024
MAX_OUTPUT = 512 * 1024


def host_limits():
    """Fail closed before source bytes; a symbol alone is not readiness proof."""
    try:
        import resource
        ceiling = 256 * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (ceiling, ceiling))
        resource.setrlimit(resource.RLIMIT_CPU, (10, 10))
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        if (resource.getrlimit(resource.RLIMIT_AS) != (ceiling, ceiling)
            or resource.getrlimit(resource.RLIMIT_CPU) != (10, 10)
            or resource.getrlimit(resource.RLIMIT_CORE) != (0, 0)):
            return None
        positive = mmap.mmap(-1, 8 * 1024 * 1024); positive.close()
        try:
            over = mmap.mmap(-1, ceiling + mmap.PAGESIZE)
        except MemoryError:
            pass
        except OSError as exc:
            if exc.errno != errno.ENOMEM: return None
        else:
            over.close(); return None
        signal.signal(signal.SIGALRM, signal.SIG_DFL)
        signal.setitimer(signal.ITIMER_REAL, 30)
        return {"memory_bytes": ceiling, "cpu_seconds": 10, "wall_seconds": 30,
            "positive_mapping_bytes": 8 * 1024 * 1024, "above_budget_denied": True,
            "no_os_sandbox_claim": True}
    except (ImportError, AttributeError, OSError, ValueError):
        return None


def read_exact(size):
    data = sys.stdin.buffer.read(size)
    if len(data) != size: raise ValueError("document_source_pipe_incomplete")
    return data


def main():
    limits = host_limits()
    if limits is None:
        print(json.dumps({"state": "blocked", "reason": "document_resource_self_check_failed"}), flush=True)
        return 2
    if len(sys.argv) != 2 or len(sys.argv[1]) != 32 or any(c not in "0123456789abcdef" for c in sys.argv[1]):
        return 2
    print(json.dumps({"state": "ready", "nonce": sys.argv[1], "limits": limits}), flush=True)
    try:
        pdf_size, csv_size = struct.unpack("!II", read_exact(8))
        if not 1 <= pdf_size <= 2 * 1024 * 1024 or not 1 <= csv_size <= 1024 * 1024 or pdf_size+csv_size > MAX_PAIR:
            raise ValueError("document_source_pipe_bound")
        pdf, csv_bytes = read_exact(pdf_size), read_exact(csv_size)
        if sys.stdin.buffer.read(1): raise ValueError("document_source_pipe_extra_bytes")
        # Trusted sibling source is fixed by this entry point; callers provide
        # neither a module path nor code. Resource checks precede this import.
        spec = importlib.util.spec_from_file_location("document_compare_parser", Path(__file__).with_name("document_compare_parser.py"))
        parser = importlib.util.module_from_spec(spec); spec.loader.exec_module(parser)
        result = parser.compare(pdf, csv_bytes)
        output = parser.canonical({"status": "succeeded", "result": result})
        if len(output) > MAX_OUTPUT: raise ValueError("document_output_pipe_bound")
    except Exception as exc:
        # Only our fixed parser errors carry operator-visible reason codes.
        reason = str(exc) if exc.__class__.__name__ == "DocumentParseError" else "document_parser_failed"
        output = json.dumps({"status": "blocked", "reason": reason, "no_learning": True}).encode()
    sys.stdout.buffer.write(output); sys.stdout.buffer.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
