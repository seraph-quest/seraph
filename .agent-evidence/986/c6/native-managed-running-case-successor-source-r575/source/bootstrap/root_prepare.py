"Root's concrete filesystem preparation entrypoint; never invoked by pytest."
import hashlib
import importlib.util
import json
import os
import sys
from pathlib import Path

PACKET = Path('/home/pawel/repos/seraph/.agent-evidence/986/c6/n12/bootstrap')
OBSERVER = Path('/home/pawel/repos/seraph/.agent-evidence/986/c6/adr032-r71/child-observer-visibility-r80')


def exact_helper(path, digest, name):
    if path.resolve(strict=True) != path or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
        raise ValueError('reviewed_preparation_helper_changed')
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)  # Only reviewed filesystem helper/stdlib.
    return module


def prepare_case(manifest, *, manifest_sha256, private_secret, materializer_sha256,
                 observer_request=None):
    'Root supplies reviewed literals; missing inputs return before writes.\n\n    An optional actual R80 source-only copy must target a separate durable\n    Root-prepared staging parent, NOT the managed destination. Its output is\n    consumed by the final manifest; unchanged final hashes must match that map.\n    '
    materializer = exact_helper(PACKET / 'materializer.py', materializer_sha256, '_root_exact_materializer')
    missing = materializer.missing_inputs(manifest)
    if missing:
        return {'prepared': False, 'missing_inputs': missing}
    canonical = json.dumps(manifest, sort_keys=True, separators=(',', ':')).encode()
    if hashlib.sha256(canonical).hexdigest() != manifest_sha256:
        raise ValueError('reviewed_case_manifest_changed')
    cache_path = PACKET / 'cache_inputs.py'
    cache_pin = manifest['bootstrap_pins'].get(str(cache_path))
    if cache_pin is None:
        return {'prepared': False, 'missing_inputs': ['bootstrap_pins:cache_inputs.py']}
    exact_helper(cache_path, cache_pin, 'cache_inputs')
    if observer_request is not None:
        request_sha = hashlib.sha256(json.dumps(observer_request, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        if request_sha != manifest['native_observer']['request_sha256']:
            raise ValueError('reviewed_R80_request_changed')
        required = {'source_root', 'destination_root', 'source_map_path', 'cutpoint',
                    'delay_seconds', 'diagnostic_directory', 'physical_workspace_root', 'case_id'}
        if set(observer_request) != required or observer_request['case_id'] != manifest['case_id']:
            raise ValueError('closed_R80_request_required')
        root = Path('/home/pawel/repos/seraph/.agent-evidence/986/c6/n12/managed-observed-epochs')
        destination = root / manifest['case_id'] / 'backend'
        if (observer_request['destination_root'] != str(destination)
                or observer_request['physical_workspace_root'] != str(materializer.PHYSICAL)
                or observer_request['diagnostic_directory'] != str(root / manifest['case_id'] / 'diagnostics')):
            raise ValueError('literal_R80_staging_context_required')
        # Existing parent ownership is a reviewed input. No hidden directories
        # or broad tree creation; R80 itself checks fresh destination/diagnostics.
        materializer.private_directory(destination.parent)
        materializer.private_directory(Path(observer_request['diagnostic_directory']))
        generator = exact_helper(OBSERVER / 'prepare_observed_epoch.py',
            'aa46d81226ad0172779ef1b599d79ae858e3516411c880d411b0bd2fd9994987', '_root_exact_R80')
        materializer.read(OBSERVER / 'observer_hook.py',
            '109b6f0458a4c1abdae8f4aa84741f7cd4c4ca773265d6490f9ac4873151d391')
        generator.prepare_observed_epoch(**observer_request)
        # Final materializer rehashes every member from that literal staged
        # source against the pre-reviewed final map; no captured-after oracle.
    return materializer.prepare(manifest, manifest_sha256=manifest_sha256, private_secret=private_secret)


def seal_preopened_stdio(context, *, controller_log_fd, backend_log_fd, devnull_fd,
                        role_stdio_sha256):
    'Root calls AFTER real materialization, BEFORE each managed generation.\n\n    No process starts here. Exact file handles remain retained by Root. Final\n    context SHA is a realized readback requiring Root sealing, not a guessed\n    future pipe/inode. The test loader subsequently uses this actual context.\n    '
    helper = exact_helper(PACKET / 'role_stdio.py', role_stdio_sha256, '_root_exact_role_stdio')
    sealed = helper.seal(context, controller_log_fd=controller_log_fd,
                         backend_log_fd=backend_log_fd, devnull_fd=devnull_fd)
    fixture = Path('/home/pawel/repos/seraph/.agent-evidence/986/c6/n12/managed-execution')
    raw = (json.dumps(sealed, sort_keys=True) + '\n').encode()
    path = fixture / 'context.pre-spawn.json'
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        offset = 0
        while offset < len(raw):
            count = os.write(fd, raw[offset:])
            if count <= 0:
                raise OSError('stdio_context_write_incomplete')
            offset += count
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(path, fixture / 'context.json')
    return {'context_sha256': hashlib.sha256(raw).hexdigest(), 'context': sealed,
            'execution_authorized': False, 'actual_preopened_stdio': True}
