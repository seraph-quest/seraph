'Finite shell-literal dotenv grammar, matching original set-a/source semantics.'
import os
from pathlib import Path
import re
import stat

ALLOWED = {'LOCAL_WORKSPACE_DIR', 'LOCAL_LLM_LOG_DIR', 'LOCAL_UV_CACHE_DIR',
    'LOCAL_BACKEND_PORT', 'LOCAL_FRONTEND_PORT', 'DAEMON_ENABLED', 'PROXY_ENABLED',
    'OPERATOR_AUTH_SECRET', 'OPERATOR_AUTH_ALLOWED_HOSTS', 'OPERATOR_AUTH_ALLOWED_ORIGINS',
    'OPERATOR_AUTH_IDLE_SECONDS', 'OPERATOR_AUTH_ABSOLUTE_SECONDS',
    'OPERATOR_AUTH_TRUSTED_PROXY_IPS', 'OPERATOR_AUTH_ALLOW_UNAUTHENTICATED_TESTS',
    'OPENAI_API_KEY', 'ANTHROPIC_API_KEY', 'OPENROUTER_API_KEY', 'LLM_API_KEY',
    'FALLBACK_LLM_API_KEY', 'LOCAL_LLM_API_KEY'}
CREDENTIALS = {'OPENAI_API_KEY', 'ANTHROPIC_API_KEY', 'OPENROUTER_API_KEY',
               'LLM_API_KEY', 'FALLBACK_LLM_API_KEY', 'LOCAL_LLM_API_KEY'}


def private_file(path):
    path = Path(path)
    info = path.lstat()
    if (not path.is_absolute() or path.resolve(strict=True) != path
            or not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or info.st_nlink != 1 or info.st_mode & 0o077):
        raise RuntimeError('private_dotenv_metadata')
    return path


def read_dotenv(path):
    values = {}
    for line in private_file(path).read_text().splitlines():
        if not line.strip() or line.startswith('#'):
            continue
        match = re.fullmatch('([A-Z_][A-Z0-9_]*)=(\'([^\']*)\'|\\"([^\\"$`\\\\]*)\\"|([A-Za-z0-9_./:@,-]*))', line)
        if not match or match[1] not in ALLOWED or match[1] in values:
            raise RuntimeError('unreviewed_dotenv_assignment')
        value = next(v for v in match.groups()[2:] if v is not None)
        if match[1] in CREDENTIALS and value:
            raise RuntimeError('nonempty_provider_localmodel_credential')
        values[match[1]] = value
    required = {'LOCAL_WORKSPACE_DIR', 'LOCAL_LLM_LOG_DIR', 'LOCAL_UV_CACHE_DIR',
                'LOCAL_BACKEND_PORT', 'LOCAL_FRONTEND_PORT', 'OPERATOR_AUTH_SECRET'}
    if not required <= values.keys() or len(values['OPERATOR_AUTH_SECRET']) < 32:
        raise RuntimeError('incomplete_private_dotenv')
    if values.get('DAEMON_ENABLED') != 'false' or values.get('PROXY_ENABLED') != 'false':
        raise RuntimeError('unexpected_managed_service')
    if values.get('OPERATOR_AUTH_ALLOW_UNAUTHENTICATED_TESTS') != 'false':
        raise RuntimeError('unauthenticated_fixture_forbidden')
    if values.get('OPERATOR_AUTH_IDLE_SECONDS') != '30' or values.get('OPERATOR_AUTH_ABSOLUTE_SECONDS') != '3600':
        raise RuntimeError('reviewed_real_auth_expiry_window_required')
    for name in ('LOCAL_WORKSPACE_DIR', 'LOCAL_LLM_LOG_DIR', 'LOCAL_UV_CACHE_DIR'):
        if not Path(values[name]).is_absolute() or Path(values[name]).resolve() != Path(values[name]):
            raise RuntimeError('noncanonical_private_context_path')
    return values


def check_loaded_environment(values, environment):
    if any(environment.get(name) != value for name, value in values.items()):
        raise RuntimeError('actual_sourced_environment_drift')
    if any(environment.get(name, '') for name in CREDENTIALS):
        raise RuntimeError('ambient_provider_localmodel_credential')
