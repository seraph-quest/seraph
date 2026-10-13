'Real operator HTTP client for reviewed private managed fixtures.\n\nNo ASGITransport, environment credential, fake principal or service substitution.\nConstruction opens no socket. Admission/contact methods require explicit calls.\n'
from http.cookiejar import CookieJar
import copy
import json
import os
from pathlib import Path
import re
import stat
from urllib.error import HTTPError
from urllib.request import (HTTPCookieProcessor, HTTPRedirectHandler, ProxyHandler,
                            Request, build_opener)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise PermissionError('managed_redirect_denied')


class OperatorHTTP:
    def __init__(self, *, backend_port, frontend_port, private_secret_file):
        self.base = 'http://127.0.0.1:' + str(backend_port)
        self.origin = 'http://127.0.0.1:' + str(frontend_port)
        self.secret_path = Path(private_secret_file)
        self.cookies = CookieJar()
        self.opener = build_opener(ProxyHandler({}), NoRedirect(), HTTPCookieProcessor(self.cookies))
        self.original_session = None

    def request(self, method, route, payload=None, expected=200):
        if not route.startswith('/api/') or any(v in route for v in ['?', '#', '\\', '%', '..']):
            raise PermissionError('managed_route_invalid')
        request = Request(self.base + route, method=method,
            data=None if payload is None else json.dumps(payload).encode(),
            headers={'Origin': self.origin, 'Content-Type': 'application/json'})
        try:
            response = self.opener.open(request, timeout=5)
        except HTTPError as error:
            response = error
        with response:
            allowed = expected if isinstance(expected, tuple) else (expected,)
            if response.status not in allowed:
                # Never put password, cookie, actor IDs or API body in failures.
                raise AssertionError('managed HTTP status ' + str(response.status))
            raw = response.read(1048577)
            if len(raw) > 1048576:
                raise AssertionError('managed HTTP body exceeds bound')
            return json.loads(raw) if raw else None

    def clone(self, *, empty=False):
        other = OperatorHTTP(backend_port=18241, frontend_port=13241, private_secret_file=self.secret_path)
        if not empty:
            for cookie in self.cookies:
                other.cookies.set_cookie(copy.copy(cookie))
            other.original_session = self.original_session
        return other

    def login(self, *, start_new_scope=False):
        info = self.secret_path.lstat()
        if (self.secret_path.resolve(strict=True) != self.secret_path
                or not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_nlink != 1 or info.st_mode & 0o077):
            raise PermissionError('private_fresh_secret_required')
        password = self.secret_path.read_text().strip()
        if len(password) < 32:
            raise PermissionError('private_local_secret_too_short')
        session = self.request('POST', '/api/auth/login', {'password': password, 'start_new_scope': start_new_scope})
        if session.get('authenticated') is not True:
            raise AssertionError('original_authentication_missing')
        self.original_session = session
        return session

    def refresh_original_session(self):
        if self.original_session is None:
            raise AssertionError('login_required')
        fresh = self.request('POST', '/api/auth/refresh')
        for field in ('principal_id', 'session_id', 'operator_identity_id', 'absolute_expires_at'):
            if fresh[field] != self.original_session[field]:
                raise AssertionError('original_session_authority_changed:' + field)
        return fresh

    def current_session(self):
        return self.request('GET', '/api/auth/session')

    def stock_tools(self):
        return self.request('GET', '/api/general-tasks/tools')

    def create_reviewed_goal(self, original_goal_request):
        return self.request('POST', '/api/goals', original_goal_request)

    def create_original_repository_task(self, *, goal_ref, goal_revision,
            original_repository_input, original_task_limits, idempotency_key):
        snapshot = self.stock_tools()
        matches = [tool for tool in snapshot['tools'] if tool['tool_id'] == 'repository_work']
        if len(matches) != 1:
            raise AssertionError('stock_repository_descriptor_unavailable')
        descriptor = matches[0]
        # Canonical descriptor comes from real stock registry, not a fixed view.
        payload = {'goal_revision': goal_revision, 'idempotency_key': idempotency_key,
            'expected_plan_revision': 1, 'accept': True,
            'input': {'goal_ref': goal_ref, 'intent': 'Repair the selected repository',
                'requested_output': descriptor['output_schema'],
                'limits': original_task_limits, 'tool_set_digest': snapshot['tool_set_digest']},
            'plan': {'revision': 1, 'steps': [{'step_id': 'repair', 'tool_id': 'repository_work',
                'input': original_repository_input, 'output_contract': descriptor['output_schema']}]}}
        return self.create_reviewed_task(payload)

    def create_reviewed_task(self, original_typed_request):
        # GeneralTaskCreate is checked by the actual endpoint. The request must
        # bind an authentic original Goal, stock descriptor and reviewed Plan.
        return self.request('POST', '/api/general-tasks', original_typed_request)

    @staticmethod
    def identifier(value):
        if not re.fullmatch('[A-Za-z0-9:_-]{1,160}', value):
            raise PermissionError('managed_identifier_invalid')
        return value

    def task(self, task_id):
        return self.request('GET', '/api/tasks/' + self.identifier(task_id))

    def task_plan(self, task_id):
        return self.request('GET', '/api/tasks/' + self.identifier(task_id) + '/plan')

    def repo(self, job_id, *, preview=False):
        route = '/api/workflows/repo-repair/' + self.identifier(job_id)
        return self.request('GET', route + ('/source-preview' if preview else ''))

    def acknowledge_reviewed_iteration(self, job_id, preview, *, idempotency_key):
        review, egress, packet = preview['repository_review'], preview['egress'], preview['source_packet']
        payload = {'expected_job_revision': preview['revision'],
            'source_packet_digest': packet['artifact_sha256'],
            'expected_source_manifest_digest': packet['source_manifest_sha256'],
            'expected_profile_id': egress['effective_profile_id'],
            'acknowledged_selected_source': True, 'acknowledged_diagnostics': True,
            'idempotency_key': idempotency_key,
            'expected_iteration_index': review['iteration_index'],
            'expected_iteration_id': review['iteration_id'],
            'expected_preparation_digest': review['preparation_digest']}
        for name in ('request_body_digest', 'request_route_digest', 'egress_envelope_digest',
                     'diagnostics_digest', 'redaction_version'):
            payload['expected_' + name] = egress[name]
        return self.request('POST', '/api/workflows/repo-repair/' + self.identifier(job_id)
                            + '/code-egress-consent', payload)

    def assert_public_recovery_held(self, job_id, *, revision):
        before = self.repo(job_id)
        assert before['revision'] == revision
        packet = before['source_recovery']
        if packet is not None:
            assert packet['public_actions'] == 'unavailable'
        result = self.request('POST', '/api/workflows/repo-repair/' + self.identifier(job_id)
            + '/source-recovery', {'expected_job_revision': revision,
                                  'action': 'reconcile_original_cleanup'}, expected=409)
        expected_code = 'original_producer_registration_missing' if packet is None else 'repository_source_recovery_not_eligible'
        assert result['detail']['code'] == expected_code
        assert result['detail']['no_learning'] is True
        assert self.repo(job_id) == before

    def reconcile_original_cleanup_unknown(self, job_id):
        'One explicit public request; authoritative GET proves only held readback.'
        before = self.repo(job_id)
        assert before['status'] == 'unknown_external_effect'
        assert before['source_recovery']['public_actions'] == 'reconcile_original_cleanup'
        assert before['source_recovery']['physical_hold'] is True
        response = self.request('POST', '/api/workflows/repo-repair/' + self.identifier(job_id)
            + '/source-recovery', {'expected_job_revision': before['revision'],
                                  'action': 'reconcile_original_cleanup'}, expected=200)
        current = self.repo(job_id)
        assert current['no_learning'] is True
        assert current['status'] == 'unknown_external_effect'
        assert current['source_recovery']['state'] == 'original_cleanup_committed'
        assert current['source_recovery']['physical_hold'] is True
        assert current['source_recovery']['public_actions'] == 'unavailable'
        return before, response, current
