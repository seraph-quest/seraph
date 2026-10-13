'Verify the exact trusted browser choice BEFORE Playwright/process startup.'
import hashlib
import json
from pathlib import Path

EXECUTABLES = {
    '/home/pawel/.cache/ms-playwright/chromium-1208/chrome-linux64/chrome',
    '/home/pawel/.cache/ms-playwright/chromium_headless_shell-1208/chrome-headless-shell-linux64/headless_shell'}
ARGS = {'--disable-background-networking', '--disable-component-update', '--no-first-run',
        '--no-default-browser-check', '--disable-sync', '--metrics-recording-only', '--no-sandbox'}


def validate(context):
    from cache_inputs import read_cache
    path = context['reviewed_browser_executable']
    args = context['reviewed_browser_args']
    if path not in EXECUTABLES or Path(path).resolve(strict=True) != Path(path):
        raise RuntimeError('literal_cached_browser_required')
    if (not isinstance(args, list) or any(type(arg) is not str or arg not in ARGS for arg in args)
            or len(set(args)) != len(args)
            or hashlib.sha256(json.dumps(args, separators=(',', ':')).encode()).hexdigest() != context['browser_args_sha256']):
        raise RuntimeError('closed_reviewed_browser_args_required')
    expected = context['browser_pins'].get(path)
    record = context['readonly_cache_metadata'].get(path)
    if expected is None or record is None or record['sha256'] != expected:
        raise RuntimeError('selected_browser_not_pinned')
    read_cache(path, record)
    return path, args
