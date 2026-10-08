"""Fixed process-profile limits in an isolated, already-executed child.

No backend imports, shell, extra process, or Python callback between fork and
exec. The target replaces this interpreter and retains its PID/session/group.
"""

import os
import signal
import sys


def apply_process_limits(ceilings):
    import resource

    for name, requested in zip(
        ("RLIMIT_CPU", "RLIMIT_AS", "RLIMIT_NPROC", "RLIMIT_FSIZE"), ceilings
    ):
        try:
            limit = getattr(resource, name)
            current_soft, current_hard = resource.getrlimit(limit)
            if name == "RLIMIT_NPROC":
                # Preserve the existing host-aware policy: finite soft limits
                # stay at least as high; unlimited hosts use the fixed fallback.
                requested = max(requested, current_soft) if current_soft != resource.RLIM_INFINITY else 1024
            hard = current_hard if current_hard != resource.RLIM_INFINITY else requested
            resource.setrlimit(limit, (min(requested, hard), hard))
        except (AttributeError, OSError, ValueError):
            # Keep the existing best-effort policy without leaking exception
            # text, target arguments, environment, or filesystem paths.
            print("process_resource_limit_unavailable:" + name, file=sys.stderr)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    try:
        if len(argv) < 6 or argv[4] != "--" or not argv[5]:
            raise ValueError
        ceilings = tuple(int(value) for value in argv[:4])
        if any(value <= 0 for value in ceilings):
            raise ValueError
    except (TypeError, ValueError):
        print("process_limit_bootstrap_invalid", file=sys.stderr)
        return 125
    apply_process_limits(ceilings)
    # Python ignores these on startup; restore Popen's original
    # restore_signals=True behavior before replacing Python with the target.
    for name in ("SIGPIPE", "SIGXFZ", "SIGXFSZ"):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), signal.SIG_DFL)
    try:
        os.execvpe(argv[5], argv[5:], os.environ)
    except OSError:
        print("process_target_exec_unavailable", file=sys.stderr)
        return 127


if __name__ == "__main__":
    sys.exit(main())
