"""Separate-origin, ticket-gated browser proxy for the local BloodHound CE UI."""
from __future__ import annotations

import atexit
import http.client
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import secrets
import threading
import time
import urllib.parse

from rmm_settings import getenv

MAX_BODY = 64 * 1024 * 1024
HOP = {'connection', 'keep-alive', 'proxy-authenticate', 'proxy-authorization',
       'te', 'trailer', 'transfer-encoding', 'upgrade'}


def endpoint(value, local_http=False):
    """Validate an operator-configured origin, never a browser-supplied destination."""
    parsed = urllib.parse.urlsplit(value.rstrip('/'))
    if (parsed.scheme not in ('http', 'https') or not parsed.hostname or
            parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment):
        raise ValueError('invalid_bloodhound_ui_endpoint')
    _ = parsed.port
    if local_http and parsed.scheme == 'http' and parsed.hostname not in ('localhost', '127.0.0.1', '::1'):
        raise ValueError('bloodhound_ui_upstream_requires_https_or_loopback')
    return parsed


class BrowserProxy(ThreadingHTTPServer):
    """Own one fixed CE upstream and short-lived browser access grants."""
    daemon_threads = True

    def __init__(self, address, upstream, public_url):
        self.upstream = endpoint(upstream, local_http=True)
        self.public = endpoint(public_url)
        self.tickets = {}
        self.sessions = {}
        self.lock = threading.Lock()
        super().__init__(address, BrowserHandler)
        self.cookie_name = 'rmm_bloodhound_proxy_' + str(self.server_port)
        self.ce_cookie_prefix = 'bhce_' + str(self.server_port) + '_'

    def connection(self):
        """Build a direct connection without environment proxies or service credentials."""
        cls = http.client.HTTPSConnection if self.upstream.scheme == 'https' else http.client.HTTPConnection
        return cls(self.upstream.hostname, self.upstream.port, timeout=30)

    def grant(self):
        """Mint a bounded single-use browser launch ticket after operator authentication."""
        with self.lock:
            self._prune()
            if len(self.tickets) >= 128 or len(self.sessions) >= 128:
                raise ValueError('bloodhound_ui_capacity')
            token = secrets.token_urlsafe(32)
            self.tickets[token] = time.monotonic() + 60
        return self.public.geturl() + '/_rmm_launch?ticket=' + token

    def _prune(self):
        """Expire grants under the caller-held lock."""
        now = time.monotonic()
        for entries in (self.tickets, self.sessions):
            for token, expiry in list(entries.items()):
                if expiry <= now:
                    entries.pop(token, None)

    def exchange(self, token):
        """Consume one ticket and issue an eight-hour proxy session."""
        with self.lock:
            self._prune()
            if self.tickets.pop(token, None) is None or len(self.sessions) >= 128:
                return None
            session = secrets.token_urlsafe(32)
            self.sessions[session] = time.monotonic() + 8 * 3600
            return session

    def valid_session(self, token):
        """Check an opaque proxy session without refreshing its expiry."""
        with self.lock:
            self._prune()
            return token in self.sessions


class BrowserHandler(BaseHTTPRequestHandler):
    """Proxy browser requests with independent admission and normal CE authentication."""
    protocol_version = 'HTTP/1.0'

    def setup(self):
        """Bound idle request/header reads before the HTTP parser runs."""
        self.request.settimeout(30)
        super().setup()

    def log_message(self, *_args):
        """Never log launch tickets, request URLs, or browser credentials."""

    def reply(self, code, message):
        """Return a short safe proxy error without backend diagnostics."""
        raw = message.encode()
        self.send_response(code)
        self.send_header('Content-Type', 'text/plain; charset=utf-8')
        self.send_header('Content-Length', str(len(raw)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(raw)

    def dispatch(self):
        """Validate origin/admission, then forward one bounded HTTP request."""
        self.connection.settimeout(30)
        if self.headers.get('Host', '').lower() != self.server.public.netloc.lower():
            return self.reply(421, 'Unexpected proxy host')
        if not self.path.startswith('/') or self.path.startswith('//') or '\\' in self.path:
            return self.reply(400, 'Invalid request target')
        parsed = urllib.parse.urlsplit(self.path)
        if parsed.path == '/_rmm_launch':
            if self.command != 'GET':
                return self.reply(405, 'GET required')
            values = urllib.parse.parse_qs(parsed.query)
            session = self.server.exchange(values.get('ticket', [''])[0])
            if not session:
                return self.reply(401, 'Launch expired; reopen BloodHound from the WebUI')
            self.send_response(303)
            self.send_header('Location', '/ui/')
            cookie = f'{self.server.cookie_name}={session}; Path=/; HttpOnly; SameSite=Lax; Max-Age=28800'
            if self.server.public.scheme == 'https':
                cookie += '; Secure'
            self.send_header('Set-Cookie', cookie)
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('Content-Length', '0')
            self.end_headers()
            return
        try:
            cookies = SimpleCookie(self.headers.get('Cookie', ''))
            token = cookies[self.server.cookie_name].value if self.server.cookie_name in cookies else ''
        except Exception:
            return self.reply(401, 'Invalid proxy session')
        if not self.server.valid_session(token):
            return self.reply(401, 'Open BloodHound from the WebUI first')
        if self.command not in ('GET', 'HEAD', 'OPTIONS') and self.headers.get('Origin') != self.server.public.geturl():
            return self.reply(403, 'Unexpected browser origin')
        if self.headers.get('Transfer-Encoding') or len(self.headers.get_all('Content-Length', [])) > 1:
            return self.reply(400, 'Unsupported request framing')
        try:
            length = int(self.headers.get('Content-Length', '0'))
        except ValueError:
            return self.reply(400, 'Invalid content length')
        if length < 0 or length > MAX_BODY:
            return self.reply(413, 'Request too large')
        if self.headers.get('Upgrade'):
            return self.reply(501, 'WebSocket upgrade is not supported')
        blocked = HOP | {'host', 'cookie', 'content-length', 'origin', 'referer',
                         'forwarded', 'x-forwarded-for', 'x-forwarded-host', 'x-forwarded-proto'}
        blocked.update(x.strip().lower() for x in self.headers.get('Connection', '').split(','))
        headers = {k: v for k, v in self.headers.items()
                   if k.lower() not in blocked and not k.lower().startswith('x-rmm-')}
        headers['Host'] = self.server.upstream.netloc
        headers['Origin'] = self.server.upstream.geturl()
        headers['Accept-Encoding'] = 'identity'
        prefix = self.server.ce_cookie_prefix
        remaining = '; '.join(f'{k[len(prefix):]}={v.coded_value}' for k, v in cookies.items()
                              if k.startswith(prefix) and len(k) > len(prefix))
        if remaining:
            headers['Cookie'] = remaining
        connection = self.server.connection()
        try:
            body = self.rfile.read(length) if length else None
            if body is not None and len(body) != length:
                return self.reply(400, 'Incomplete request')
            connection.request(self.command, self.path, body=body, headers=headers)
            response = connection.getresponse()
            raw = response.read(MAX_BODY + 1)
            if len(raw) > MAX_BODY:
                return self.reply(502, 'Upstream response too large')
            response_headers = []
            excluded = HOP | {'content-length', 'set-cookie', 'location', 'access-control-allow-origin',
                              'access-control-allow-credentials'}
            excluded.update(x.strip().lower() for x in (response.getheader('Connection') or '').split(','))
            location = response.getheader('Location')
            if location:
                target = urllib.parse.urlsplit(urllib.parse.urljoin(self.server.upstream.geturl() + self.path, location))
                if (target.scheme, target.netloc) != (self.server.upstream.scheme, self.server.upstream.netloc):
                    return self.reply(502, 'External upstream redirect refused')
                response_headers.append(('Location', urllib.parse.urlunsplit(('', '', target.path, target.query, target.fragment))))
            for key, value in response.getheaders():
                if key.lower() not in excluded:
                    response_headers.append((key, value))
                elif key.lower() == 'set-cookie':
                    # Namespace CE cookies; cookies otherwise cross port boundaries.
                    cookie = SimpleCookie(value)
                    for name, morsel in cookie.items():
                        namespaced = SimpleCookie()
                        namespaced[prefix + name] = morsel.value
                        for attribute, setting in morsel.items():
                            if attribute != 'domain' and setting:
                                namespaced[prefix + name][attribute] = setting
                        response_headers.append(('Set-Cookie', namespaced[prefix + name].OutputString()))
            self.send_response(response.status)
            for key, value in response_headers:
                self.send_header(key, value)
            self.send_header('Content-Length', response.getheader('Content-Length', '0')
                             if self.command == 'HEAD' else str(len(raw)))
            self.send_header('Referrer-Policy', 'no-referrer')
            self.end_headers()
            if self.command != 'HEAD':
                self.wfile.write(raw)
        except (OSError, http.client.HTTPException, ValueError):
            self.reply(502, 'BloodHound is unavailable')
        finally:
            connection.close()

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = do_OPTIONS = dispatch


_instance = None
_configuration = None
_instance_lock = threading.Lock()


def close_ui():
    """Stop the process-owned listener and invalidate browser grants on shutdown."""
    global _instance, _configuration
    with _instance_lock:
        if _instance is not None:
            _instance.shutdown()
            _instance.server_close()
            _instance = None
            _configuration = None


atexit.register(close_ui)


def open_ui():
    """Start the configured proxy lazily and mint a launch URL for an authenticated caller."""
    global _instance, _configuration
    try:
        upstream = getenv('RMM_BLOODHOUND_URL', 'http://127.0.0.1:1030')
        bind = getenv('RMM_BLOODHOUND_UI_BIND', '127.0.0.1')
        port = int(getenv('RMM_BLOODHOUND_UI_PORT', '1031'))
        public = getenv('RMM_BLOODHOUND_UI_PUBLIC_URL', f'http://127.0.0.1:{port}')
        upstream_parts = endpoint(upstream, local_http=True)
        endpoint(public)
        if (upstream.rstrip('/') == public.rstrip('/') or
                (upstream_parts.hostname in ('localhost', '127.0.0.1', '::1') and
                 (upstream_parts.port or (443 if upstream_parts.scheme == 'https' else 80)) == port)):
            raise ValueError('bloodhound_ui_proxy_loop')
        if not 1 <= port <= 65535:
            raise ValueError('invalid_bloodhound_ui_port')
        configuration = (upstream.rstrip('/'), public.rstrip('/'), bind, port)
        with _instance_lock:
            if _instance is None:
                proxy = BrowserProxy((bind, port), upstream, public)
                threading.Thread(target=proxy.serve_forever, name='bloodhound-browser-proxy', daemon=True).start()
                _instance = proxy
                _configuration = configuration
            elif _configuration != configuration:
                return 409, {'ok': False, 'error': 'bloodhound_ui_restart_required'}
            instance = _instance
        connection = instance.connection()
        connection.timeout = 3
        try:
            connection.request('GET', '/api/version')
            response = connection.getresponse()
            if response.status != 200:
                return 503, {'ok': False, 'available': False, 'error': 'bloodhound_ui_upstream_unavailable'}
        except (OSError, http.client.HTTPException):
            return 503, {'ok': False, 'available': False, 'error': 'bloodhound_ui_upstream_unavailable'}
        finally:
            connection.close()
        url = instance.grant()
        return 200, {'ok': True, 'available': True, 'url': url, 'expires_in': 60}
    except (ValueError, OSError):
        return 503, {'ok': False, 'available': False, 'error': 'bloodhound_ui_proxy_configuration_or_bind_failed'}
