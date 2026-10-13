import hashlib
import json
import os
from pathlib import Path
import secrets
import subprocess

base = Path('/home/pawel/repos/seraph/.agent-evidence/986/c6/n12')
out = base / 'preparation-inputs'
template = json.loads((out / 'input-template-r2.json').read_bytes())
pins = template['bootstrap_pins']
assert len(pins) == 37
for name, expected in pins.items():
    path = Path(name)
    assert path.parent == base / 'bootstrap'
    st = path.lstat()
    assert path.is_file() and not path.is_symlink() and st.st_uid == os.getuid() and st.st_nlink == 1
    assert hashlib.sha256(path.read_bytes()).hexdigest() == expected
base_commit = subprocess.check_output(['git', '-C', str(out / 'staged-example'), 'rev-parse', 'HEAD'], text=True).strip()
assert len(base_commit) == 40 and all(c in '0123456789abcdef' for c in base_commit)
values = dict(nonce=secrets.token_hex(32), private_secret=secrets.token_hex(32),
              source_commit='e43ad4b20dcc2ca6baa90e31ff38146f1d50c7ea', base_commit=base_commit)
flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
def write(path, value):
    with os.fdopen(os.open(path, flags, 0o600), 'w') as stream:
        stream.write(value)
        stream.flush()
        os.fsync(stream.fileno())
write(out / 'private-values.json', json.dumps(values, sort_keys=True) + '\n')
write(out / 'final-helper-pins.json', json.dumps(pins, sort_keys=True) + '\n')
write(base / 'bootstrap' / 'PINS.txt', ''.join(expected + '  ' + Path(name).name + '\n' for name, expected in sorted(pins.items())))
print(json.dumps({'fresh_private_keys': 4, 'helpers_verified': len(pins), 'exclusive_private_files': 3}))
