"""Automatic BloodHound CE credential bootstrap at the workspace storage owner.

The dedicated Exegol workspace prints a randomly generated initial admin
password to the CE launch log on the first start of a fresh database
(``# Initial Password Set To:    <password>    #`` — see ``CreateDefaultAdmin``
in BloodHound CE). The RMM launch command already captures that log inside the
container, and the workspace service runs in the same container, so it can
read the password directly, log in through CE's own login endpoint, mint a
dedicated API token, and store it in the owner-local settings store.

No credential value ever reaches a log line or an API response; failures are
reported as bounded codes only.
"""
from __future__ import annotations

import http.client
import json
import re
from pathlib import Path
from urllib.parse import urlsplit

CE_LAUNCH_LOG = '/var/log/rmm-bloodhound-ce.log'
CE_LOGIN_PATH = '/api/v2/login'
CE_TOKEN_PATH = '/api/v2/tokens'
CE_TOKEN_NAME = 'minimal-rmm'
CE_ADMIN_USER = 'admin'
# BloodHound CE generates the initial password from [A-Za-z0-9_] (32 chars)
# and prints it framed by the '# ... #' banner in the launch log.
INITIAL_PASSWORD = re.compile(r'#\s*Initial Password Set To:\s+([A-Za-z0-9_]{16,128})\s+#')
MAX_LOG_BYTES = 512 * 1024
MAX_BODY_BYTES = 64 * 1024
HTTP_TIMEOUT = 10


class CeAuthError(ValueError):
    """A public failure code that contains no credentials or endpoints."""


def read_initial_password(log_path: str = CE_LAUNCH_LOG) -> str:
    """Return the last initial admin password printed in the CE launch log.

    The log is read from its bounded tail; older banners (from previous fresh
    starts) lose to the newest one. Raises a secret-free code when the banner
    is absent — typically a database that was initialized before RMM managed
    this CE, in which case the operator resets CE or configures a token.
    """
    path = Path(log_path)
    try:
        size = path.stat().st_size
        with path.open('rb') as handle:
            if size > MAX_LOG_BYTES:
                handle.seek(-MAX_LOG_BYTES, 2)
            data = handle.read(MAX_LOG_BYTES)
    except OSError:
        raise CeAuthError('ce_launch_log_unavailable') from None
    matches = INITIAL_PASSWORD.findall(data.decode(errors='replace'))
    if not matches:
        raise CeAuthError('ce_initial_password_unavailable')
    return matches[-1]


def _origin(origin: str):
    """Validate a CE origin and return its parsed parts."""
    parsed = urlsplit(origin.rstrip('/'))
    if parsed.scheme not in ('http', 'https') or not parsed.hostname \
            or parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment:
        raise CeAuthError('ce_endpoint_invalid')
    return parsed


def _request(origin: str, method: str, path: str, payload: dict | None = None,
             headers: dict | None = None) -> tuple[int, dict | list | None]:
    """Send one bounded JSON request and return (status, decoded data)."""
    parsed = _origin(origin)
    cls = http.client.HTTPSConnection if parsed.scheme == 'https' else http.client.HTTPConnection
    connection = cls(parsed.hostname, parsed.port, timeout=HTTP_TIMEOUT)
    try:
        body = json.dumps(payload, separators=(',', ':')).encode() if payload is not None else None
        request_headers = {'Accept': 'application/json'}
        if body is not None:
            request_headers['Content-Type'] = 'application/json'
        request_headers.update(headers or {})
        connection.request(method, path, body=body, headers=request_headers)
        response = connection.getresponse()
        raw = response.read(MAX_BODY_BYTES + 1)
        if len(raw) > MAX_BODY_BYTES:
            raise CeAuthError('ce_response_too_large')
        document = json.loads(raw.decode(errors='replace')) if raw else {}
        if not isinstance(document, dict):
            raise CeAuthError('ce_response_invalid')
        return response.status, document
    except (OSError, http.client.HTTPException):
        raise CeAuthError('ce_api_unreachable') from None
    except ValueError:
        raise CeAuthError('ce_response_invalid') from None
    finally:
        connection.close()


def ce_login(origin: str, username: str, password: str) -> str:
    """Exchange CE credentials for a bearer session token."""
    status, document = _request(
        origin, 'POST', CE_LOGIN_PATH,
        {'login_method': 'secret', 'username': username, 'secret': password},
    )
    if status != 200 or not isinstance(document.get('data'), dict):
        raise CeAuthError('ce_login_rejected')
    token = document['data'].get('session_token')
    if not isinstance(token, str) or not token:
        raise CeAuthError('ce_login_rejected')
    return token


def create_api_token(origin: str, session: str, name: str = CE_TOKEN_NAME) -> tuple[str, str]:
    """Mint one dedicated signed API token and return (id, key)."""
    status, document = _request(
        origin, 'POST', CE_TOKEN_PATH, {'token_name': name},
        {'Authorization': f'bearer {session}'},
    )
    if status not in (200, 201) or not isinstance(document.get('data'), dict):
        raise CeAuthError('ce_token_creation_failed')
    row = document['data']
    token_id, token_key = row.get('id'), row.get('key')
    if not isinstance(token_id, str) or not re.fullmatch(r'[0-9a-f-]{36}', token_id) \
            or not isinstance(token_key, str) or not token_key:
        raise CeAuthError('ce_token_creation_failed')
    return token_id, token_key


def endpoint_for(origin: str) -> str:
    """Validate the CE origin and return it unchanged for direct requests."""
    _origin(origin)
    return origin.rstrip('/')


def bootstrap_credentials(origin: str, *, log_path: str = CE_LAUNCH_LOG,
                          username: str = CE_ADMIN_USER) -> dict[str, str]:
    """Derive CE credentials end to end and return them; never log any value."""
    origin = endpoint_for(origin)
    password = read_initial_password(log_path)
    session = ce_login(origin, username, password)
    token_id, token_key = create_api_token(origin, session)
    return {'username': username, 'password': password,
            'token_id': token_id, 'token_key': token_key}
