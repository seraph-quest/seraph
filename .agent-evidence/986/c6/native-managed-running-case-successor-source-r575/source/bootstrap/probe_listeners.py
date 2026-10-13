'Owned disposable dummy listeners, created only in released fixture execution.'
import json
import os
from pathlib import Path
import select
import socket


def create():
    directory = Path('/home/pawel/repos/seraph/.agent-evidence/986/c6/n12/managed-execution')
    unix = str(directory / 'dummy-provider.sock')
    tuples = [[2, '127.0.0.1', 18241], [10, '::1', 18242], [1, unix]]
    listeners, identities = [], []
    start = int(Path('/proc/self/stat').read_text().rsplit(')', 1)[1].split()[19])
    try:
        for item in tuples:
            listener = socket.socket(item[0], socket.SOCK_STREAM)
            listeners.append(listener)
            if item[0] == 10:
                listener.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
            listener.bind(item[1] if item[0] == 1 else tuple(item[1:]))
            listener.listen(1)
            info = os.fstat(listener.fileno())
            identities.append({'tuple': item, 'pid': os.getpid(), 'start': start,
                'fd': listener.fileno(), 'socket_identity': [info.st_dev, info.st_ino]})
        fd = os.open(directory / 'probe-endpoints.json', os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'w') as stream:
            json.dump(identities, stream); stream.flush(); os.fsync(stream.fileno())
        return listeners
    except BaseException:
        for listener in listeners:
            listener.close()
        raise


def assert_no_contact(listeners):
    if select.select(listeners, [], [], 0)[0]:
        raise AssertionError('forbidden_dummy_listener_received_contact')
