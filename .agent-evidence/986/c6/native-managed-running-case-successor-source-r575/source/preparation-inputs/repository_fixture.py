'Root-reviewed genuine disposable Git fixture; never a worktree or DB seed.'
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

OUT = Path(__file__).resolve().parent
ORIGINAL_PACKET = OUT
STAGE = ORIGINAL_PACKET / 'staged-example'
TARGET = Path('/tmp/seraph-1009-n12-runtime/workspace/example')
FILES = {'calculator.py': 'def add(a, b):\n    return a - b\n',
         'tests/test_calculator.py': 'from calculator import add\ndef test_add():\n    assert add(1, 2) == 3\n'}


def git(*args, root=STAGE):
    return subprocess.run(['/usr/bin/git', '-C', str(root), *args], check=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10,
        env={'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8', 'HOME': str(OUT),
             'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': '/dev/null'}).stdout.decode().strip()


def map_files(root):
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in root.rglob('*') if p.is_file() and not p.is_symlink()}


def main():
    if sys.argv[1:] == ['stage']:
        STAGE.mkdir(mode=0o700)
        (STAGE / 'tests').mkdir(mode=0o700)
        for name, text in FILES.items():
            path = STAGE / name
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, 'w') as stream:
                stream.write(text)
        git('init', '--initial-branch=fixture')
        git('config', 'user.name', 'Private fixture')
        git('config', 'user.email', 'fixture@invalid.local')
        git('add', '--', 'calculator.py', 'tests/test_calculator.py')
        git('commit', '-m', 'Original calculator fixture')
        receipt = {'head': git('rev-parse', 'HEAD'), 'files': map_files(STAGE)}
        fd = os.open(OUT / 'repository-receipt.json', os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'w') as stream:
            json.dump(receipt, stream, sort_keys=True)
        print(json.dumps({'base_commit': receipt['head']}))
    elif sys.argv[1:] == ['copy-after-prepare']:
        receipt = json.loads((ORIGINAL_PACKET / 'repository-receipt.json').read_bytes())
        if git('rev-parse', 'HEAD') != receipt['head'] or map_files(STAGE) != receipt['files']:
            raise ValueError('genuine_repository_stage_drift')
        parent = TARGET.parent
        if parent.resolve(strict=True) != parent or parent.stat().st_mode & 0o077 or parent.stat().st_uid != os.getuid():
            raise ValueError('actual_private_workspace_required')
        if TARGET.exists() or TARGET.is_symlink():
            raise ValueError('fresh_repository_destination_required')
        shutil.copytree(STAGE, TARGET, symlinks=False)
        if map_files(TARGET) != receipt['files'] or git('rev-parse', 'HEAD', root=TARGET) != receipt['head']:
            raise ValueError('full_repository_copy_or_HEAD_changed')
        print(json.dumps({'copied': True, 'actual_git_HEAD': receipt['head'], 'execution_authorized': False}))
    else:
        raise ValueError('closed_fixture_operation_required')


if __name__ == '__main__':
    main()
