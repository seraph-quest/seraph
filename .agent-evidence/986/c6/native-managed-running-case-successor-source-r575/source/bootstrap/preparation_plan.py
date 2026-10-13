'Read-only finite preparation plan. No copy, mkdir, imports or launch.'
import hashlib
import json
import os
from pathlib import Path
import re
import stat

ROOT = Path('/home/pawel/repos/seraph')
PACKET = ROOT / '.agent-evidence/986/c6/n12/bootstrap'
FIXTURE = ROOT / '.agent-evidence/986/c6/n12/managed-execution'
PHYSICAL = Path('/tmp/seraph-1009-n12-runtime')
OBSERVER = ROOT / '.agent-evidence/986/c6/adr032-r71/child-observer-visibility-r80'
SIX = ('repo_original_producer.py', 'repo_original_producer_finalizer.py', 'repo_supervisor.py',
       'repo_sandbox.py', 'repo_node.py', 'repo_worker.py')


def read(path, expected):
    path = Path(path)
    # Classify names before metadata/content; this generator reads only exact
    # reviewed source/cache pins and the explicitly named private input JSON.
    if re.search('(?i)\\.(db|sqlite|sqlite3)(-(wal|shm|journal))?$|-(wal|shm|journal)$', path.name):
        raise ValueError('database_frontier_forbidden')
    if path.resolve(strict=True) != path:
        raise ValueError('source_pin_alias_forbidden')
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise ValueError('regular_source_pin_required')
        raw = bytearray()
        while len(raw) <= before.st_size:
            block = os.read(descriptor, min(1048576, before.st_size + 1 - len(raw)))
            if not block:
                break
            raw.extend(block)
        after = os.fstat(descriptor)
        fields = ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_ctime_ns')
        if any(getattr(before, field) != getattr(after, field) for field in fields) or len(raw) != before.st_size:
            raise ValueError('source_pin_changed_while_read')
        if hashlib.sha256(raw).hexdigest() != expected:
            raise ValueError('literal_source_pin_drift')
        return bytes(raw)
    finally:
        os.close(descriptor)


def generate(inputs):
    from case_matrix import specifications
    required = {'source_commit', 'source_reference', 'source_map_path', 'source_map_sha256',
        'backend_map_path', 'backend_map_sha256', 'frontend_reference', 'frontend_map_path',
        'frontend_map_sha256', 'context_template', 'original_requests', 'case_nonces',
        'source_file_pins', 'executable_pins', 'startup_file_pins', 'browser_pins',
        'node_cache_pins', 'observer_pins', 'native_diagnostic_visibility'}
    if set(inputs) != required:
        raise ValueError('missing_or_extra_explicit_preparation_input')
    if not re.fullmatch('[0-9a-f]{40}', inputs['source_commit']):
        raise ValueError('sealed_source_commit_required')
    source_map = json.loads(read(inputs['source_map_path'], inputs['source_map_sha256']))
    backend_map = json.loads(read(inputs['backend_map_path'], inputs['backend_map_sha256']))
    frontend_map = json.loads(read(inputs['frontend_map_path'], inputs['frontend_map_sha256']))
    if not source_map or not backend_map or not frontend_map:
        raise ValueError('complete_finite_maps_required')
    for name in SIX:
        if 'src/execution/' + name not in backend_map:
            raise ValueError('canonical_parent_child_full_backend_map_required')
    for reference, mapping in ((inputs['source_reference'], source_map),
                               (Path(inputs['source_reference']) / 'backend', backend_map),
                               (inputs['frontend_reference'], frontend_map)):
        root = Path(reference)
        for name, pin in mapping.items():
            relative = Path(name)
            if relative.is_absolute() or str(relative) != name or '..' in relative.parts:
                raise ValueError('finite_map_path_invalid')
            read(root / relative, pin)
    for group in ('source_file_pins', 'executable_pins', 'startup_file_pins', 'browser_pins', 'node_cache_pins', 'observer_pins'):
        if not inputs[group]:
            raise ValueError('explicit_complete_closure_required:' + group)
        for path, pin in inputs[group].items():
            read(path, pin)
    if inputs['observer_pins'].get(str(OBSERVER / 'prepare_observed_epoch.py')) != 'aa46d81226ad0172779ef1b599d79ae858e3516411c880d411b0bd2fd9994987':
        raise ValueError('reviewed_native_generator_required')
    if inputs['observer_pins'].get(str(OBSERVER / 'observer_hook.py')) != '109b6f0458a4c1abdae8f4aa84741f7cd4c4ca773265d6490f9ac4873151d391':
        raise ValueError('reviewed_native_hook_required')
    template = inputs['context_template']
    if template['launcher_env'].get('UV_NO_SYNC') != '1':
        raise ValueError('private_managed_no_sync_required')
    for name, expected in {'source_root': str(FIXTURE / 'source'),
        'journey_directory': str(FIXTURE / 'journey'), 'receipt_dir': str(FIXTURE / 'receipts'),
        'local_secret_file': str(FIXTURE / 'operator-secret'),
        'private_controller_log': str(FIXTURE / 'controller.log')}.items():
        if template.get(name) != expected:
            raise ValueError('literal_preparation_context_path_changed:' + name)
    if template['client_ports'] != [18241, 13241] or template['python_roles'] is None:
        raise ValueError('closed_role_tuple_context_required')
    if len(template['python_roles']) != 3 or sorted(role['name'] for role in template['python_roles']) != ['backend', 'schema_guard', 'workspace_helper']:
        raise ValueError('exact_three_role_context_required')
    if not template['reviewed_frontend_process_argv'] or not template['reviewed_vite_descendant_argv']:
        raise ValueError('literal_selected_vite_chain_required')
    cases = []
    for case in specifications():
        name = case['id']
        nonce = inputs['case_nonces'][name]
        if not re.fullmatch('[0-9a-f]{64}', nonce):
            raise ValueError('explicit_unique_private_case_nonce_required')
        context = dict(template, nonce=nonce, source_file_pins=inputs['source_file_pins'],
            executable_pins=inputs['executable_pins'], startup_file_pins=inputs['startup_file_pins'])
        original = inputs['original_requests'][name]
        needed = {'goal_request', 'repository_input', 'task_limits', 'sandbox_settings',
                  'model_fabric_settings', 'model_output', 'idempotency_key'}
        if set(original) != needed:
            raise ValueError('actual_typed_original_requests_required:' + name)
        context['reviewed_case'] = dict(original, cutpoint=case['cutpoint'],
            expected_original_state=case['expected_original_state'],
            child_observer_selector=case['native_selector'])
        diagnostic = ROOT / '.agent-evidence/986/c6/n12/managed-observed-epochs' / name / 'diagnostics'
        context['native_diagnostic_directory'] = str(diagnostic)
        context['native_case_id'] = name
        steps = ['verify_all_literal_pins', 'prepare_fresh_private_physical_and_archive_parent',
                 'copy_original_manage_frontend_and_backend_from_sealed_maps',
                 'materialize_exact_keyless_dotenv_and_fresh_private_operator_secret',
                 'seal_context_and_role_stdio_before_any_spawn']
        observer = None
        blockers = []
        if case['native_selector']:
            observer = {'generator': str(OBSERVER / 'prepare_observed_epoch.py'),
                'source_root': str(Path(inputs['source_reference']) / 'backend'),
                'destination_root': str(diagnostic.parent / 'backend'),
                'source_map_path': inputs['backend_map_path'], 'cutpoint': case['native_selector'],
                'delay_seconds': 5.0, 'same_backend_parent_child_package_required': True,
                'diagnostic_directory': str(diagnostic),
                'physical_workspace_root': str(PHYSICAL), 'case_id': name,
                'seal_all_final_files_including_six_protocol_sources_before_admission': True}
            steps[2] = 'copy_original_manage_frontend_then_reviewed_generator_full_backend_epoch'
            if inputs['native_diagnostic_visibility'] != 'reviewed_R80_transient_private_marker':
                blockers.append('R80_combined_code_review_and_actual_mechanics_required')
        if case['expected_original_state'] == 'unknown_external_effect':
            blockers.append('post_R77_R78_integrated_canonical_owner_source_binding_required')
        cases.append({'specification': case, 'context': context, 'observer_materialization': observer,
                      'steps': steps, 'execution_blockers': blockers})
    if len(set(inputs['case_nonces'].values())) != len(cases):
        raise ValueError('distinct_case_nonces_required')
    return {'source_commit': inputs['source_commit'], 'private_physical_root': str(PHYSICAL),
        'fixed_source_root': str(FIXTURE / 'source'), 'sequential_fresh_case_epochs_only': True,
        'contexts_unsealed_until_postcopy_readback': True, 'cases': cases,
        'execution_authorized': False}
