'Controller-only Linux amd64 pidfd primitives; no PID signal fallback.'
import ctypes
import os
import signal
import sys


def _syscall():
    if sys.platform != 'linux' or os.uname().machine != 'x86_64' or ctypes.sizeof(ctypes.c_void_p) != 8:
        raise RuntimeError('controller_pidfd_platform_unavailable')
    library = ctypes.CDLL(None, use_errno=True)
    operation = library.syscall
    operation.restype = ctypes.c_long
    return operation


def pidfd_open(pid, flags=0):
    if type(pid) is not int or not 0 < pid <= 0x7fffffff or type(flags) is not int or flags != 0:
        raise ValueError('controller_pidfd_open_arguments')
    result = int(_syscall()(ctypes.c_long(434), ctypes.c_int(pid), ctypes.c_uint(0)))
    if result < 0:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code))
    os.set_inheritable(result, False)
    return result


def pidfd_send_signal(descriptor, signum, siginfo=None, flags=0):
    if (type(descriptor) is not int or not 0 <= descriptor <= 0x7fffffff
            or not isinstance(signum, int) or isinstance(signum, bool)
            or not 0 <= signum < signal.NSIG or siginfo is not None
            or type(flags) is not int or flags != 0):
        raise ValueError('controller_pidfd_send_arguments')
    result = int(_syscall()(ctypes.c_long(424), ctypes.c_int(descriptor), ctypes.c_int(signum),
                            ctypes.c_void_p(None), ctypes.c_uint(0)))
    if result < 0:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code))


def install():
    'Validate real kernel capability before the first original Popen.'
    _syscall()
    open_operation = getattr(os, 'pidfd_open', pidfd_open)
    send_operation = getattr(signal, 'pidfd_send_signal', pidfd_send_signal)
    descriptor = open_operation(os.getpid(), 0)
    try:
        send_operation(descriptor, 0, None, 0)
    finally:
        os.close(descriptor)
    if not hasattr(os, 'pidfd_open'):
        os.pidfd_open = pidfd_open
    if not hasattr(signal, 'pidfd_send_signal'):
        signal.pidfd_send_signal = pidfd_send_signal
    return {'pidfd_open': 'stdlib' if open_operation is not pidfd_open else 'linux_amd64_syscall_434',
            'pidfd_send_signal': 'stdlib' if send_operation is not pidfd_send_signal else 'linux_amd64_syscall_424',
            'self_descriptor_capability_verified': True, 'application_inheritance_claim': False}
