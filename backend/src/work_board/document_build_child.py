"""Fixed renderer entry point: confined before bounded postclaim content input."""
from __future__ import annotations
import json
from pathlib import Path
import struct
import sys

PROFILE = 'document-build-renderer.v1'


def main():
    # Fixed package location; no caller module/path or executable selection.
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from src.work_board.document_read_child import confine
    try:
        confine()
    except (RuntimeError, OSError, ImportError):
        return 2
    if len(sys.argv) != 2 or len(sys.argv[1]) != 32 or any(c not in '0123456789abcdef' for c in sys.argv[1]):
        return 2
    print(json.dumps({'state': 'ready', 'nonce': sys.argv[1], 'network_denied': True, 'profile': PROFILE}), flush=True)
    def exact(size):
        data = sys.stdin.buffer.read(size)
        if len(data) != size:
            raise ValueError('document_build_pipe_incomplete')
        return data
    try:
        spec_size, selected_size = struct.unpack('!II', exact(8))
        if not 0 < spec_size <= 65536 or not 0 <= selected_size <= 16384:
            raise ValueError('document_build_pipe_bound')
        raw, selected = exact(spec_size), exact(selected_size)
        if sys.stdin.buffer.read(1):
            raise ValueError('document_build_pipe_extra_bytes')
        from src.work_board.document_build_contracts import DocumentBuildSpec
        from src.work_board.document_build_renderer import render
        result = render(DocumentBuildSpec.model_validate_json(raw), selected_view=selected or None)
        editable, pdf = result.editable, result.pdf or b''
        metadata = {'status': 'succeeded', 'editable_media': result.editable_media, 'editable_extension': result.editable_extension, 'warnings': result.warnings, 'source_refs': result.source_refs, 'profile': PROFILE, 'provider_contacts': 0, 'no_learning': True}
    except Exception:
        editable, pdf = b'', b''
        metadata = {'status': 'blocked', 'reason': 'document_build_render_failed', 'profile': PROFILE, 'provider_contacts': 0, 'no_learning': True}
    encoded = json.dumps(metadata, sort_keys=True, separators=(',', ':')).encode()
    if len(encoded) > 65536:
        return 2
    sys.stdout.buffer.write(struct.pack('!III', len(editable), len(pdf), len(encoded)) + editable + pdf + encoded)
    sys.stdout.buffer.flush()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
