"""Browser admission and forwarding contract tests for the CE proxy."""
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
import unittest
from unittest.mock import Mock, patch

from rmm_bloodhound_proxy import BrowserProxy, endpoint, open_ui


class Upstream(BaseHTTPRequestHandler):
    received = []

    def log_message(self, *_args):
        pass

    def do_GET(self):
        type(self).received.append((self.path, dict(self.headers)))
        self.send_response(302 if self.path == '/external' else 200)
        if self.path == '/external':
            self.send_header('Location', 'https://evil.invalid/')
        self.send_header('Content-Type', 'text/plain')
        self.send_header('Connection', 'X-Private')
        self.send_header('X-Private', 'secret')
        self.send_header('Set-Cookie', 'ce_session=new; Path=/; HttpOnly; Domain=localhost')
        self.end_headers()
        self.wfile.write(b'CE')

    do_POST = do_GET


class ProxyTests(unittest.TestCase):
    def setUp(self):
        self.upstream = ThreadingHTTPServer(('127.0.0.1', 0), Upstream)
        self.proxy = BrowserProxy(('127.0.0.1', 0),
                                  f'http://127.0.0.1:{self.upstream.server_port}', 'http://127.0.0.1:1')
        self.proxy.public = endpoint(f'http://127.0.0.1:{self.proxy.server_port}')
        for server in (self.upstream, self.proxy):
            threading.Thread(target=server.serve_forever, daemon=True).start()
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)
        Upstream.received = []

    def request(self, path, method='GET', headers=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.proxy.server_port)
        connection.request(method, path, headers=headers or {})
        response = connection.getresponse()
        result = (response.status, dict(response.getheaders()), response.read())
        connection.close()
        return result

    def login(self):
        url = self.proxy.grant()
        path = url.removeprefix(self.proxy.public.geturl())
        status, headers, _ = self.request(path)
        self.assertEqual(status, 303)
        self.assertIn('HttpOnly', headers['Set-Cookie'])
        self.assertEqual(headers['Location'], '/ui/')
        self.assertEqual(self.request(path)[0], 401)
        return headers['Set-Cookie'].split(';')[0]

    def test_admission_host_and_ticket_expiry(self):
        self.assertEqual(self.request('/ui/')[0], 401)
        self.assertEqual(self.request('/ui/', headers={'Host': 'evil.invalid'})[0], 421)
        url = self.proxy.grant()
        ticket = url.split('ticket=')[1]
        self.proxy.tickets[ticket] = 0
        self.assertEqual(self.request('/_rmm_launch?ticket=' + ticket)[0], 401)

    def test_proxy_preserves_ce_auth_and_strips_admission_and_hop_headers(self):
        cookie = self.login()
        status, headers, body = self.request('/ui/', headers={
            'Cookie': cookie + '; other_app=secret; rmm_other=secret; ' + self.proxy.ce_cookie_prefix + 'ce_session=abc',
            'Authorization': 'Bearer ce-token', 'X-RMM-Token': 'rmm-secret',
            'Connection': 'X-Private', 'X-Private': 'secret'})
        self.assertEqual((status, body), (200, b'CE'))
        upstream = Upstream.received[-1][1]
        self.assertEqual(upstream['Authorization'], 'Bearer ce-token')
        self.assertEqual(upstream['Cookie'], 'ce_session=abc')
        self.assertNotIn('X-RMM-Token', upstream)
        self.assertNotIn('X-Private', upstream)
        self.assertNotIn('X-Private', headers)
        self.assertIn(self.proxy.ce_cookie_prefix + 'ce_session=new', headers['Set-Cookie'])
        self.assertNotIn('Domain=', headers['Set-Cookie'])

    def test_mutations_require_origin_and_external_redirect_refused(self):
        headers = {'Cookie': self.login()}
        self.assertEqual(self.request('/api/test', 'POST', headers)[0], 403)
        headers['Origin'] = self.proxy.public.geturl()
        self.assertEqual(self.request('/api/test', 'POST', headers)[0], 200)
        self.assertEqual(self.request('/external', headers=headers)[0], 502)
        headers['Transfer-Encoding'] = 'chunked'
        self.assertEqual(self.request('/api/test', 'POST', headers)[0], 400)

    def test_endpoint_constraints(self):
        for bad in ('http://user:pass@localhost', 'http://localhost/path', 'file:///tmp/a',
                    'http://localhost/?q=1', 'http://remote.invalid'):
            with self.assertRaises(ValueError):
                endpoint(bad, local_http=True)

    def test_duplicate_length_rejected_before_forwarding(self):
        cookie = self.login()
        connection = http.client.HTTPConnection('127.0.0.1', self.proxy.server_port)
        connection.putrequest('POST', '/api/test')
        connection.putheader('Cookie', cookie)
        connection.putheader('Origin', self.proxy.public.geturl())
        connection.putheader('Content-Length', '0')
        connection.putheader('Content-Length', '0')
        connection.endheaders()
        response = connection.getresponse()
        self.assertEqual(response.status, 400)
        response.read()
        connection.close()
        self.assertEqual(Upstream.received, [])


class LaunchTests(unittest.TestCase):
    def test_readiness_and_reuse_without_api_credentials(self):
        proxy = Mock()
        proxy.grant.return_value = 'http://localhost:1031/_rmm_launch?ticket=opaque'
        with patch('rmm_bloodhound_proxy._instance', None), \
                patch('rmm_bloodhound_proxy._configuration', None), \
                patch('rmm_bloodhound_proxy.BrowserProxy', return_value=proxy) as constructor, \
                patch('rmm_bloodhound_proxy.threading.Thread'), \
                patch('rmm_bloodhound_proxy.getenv', side_effect=lambda key, default: 'localhost' if key.endswith('_BIND') else default), \
                patch('rmm_bloodhound_proxy.probe_ce', side_effect=['ready', 'authentication_failed', 'ready', 'unavailable']):
            self.assertEqual(open_ui()[0], 200)
            # A CE answering 401 to the credential-free probe is up and proxied.
            self.assertEqual(open_ui()[0], 200)
            self.assertEqual(open_ui()[0], 200)
            constructor.assert_called_once()
            code, result = open_ui()
            self.assertEqual(code, 503)
            self.assertFalse(result['available'])
            self.assertEqual(proxy.grant.call_count, 3)


if __name__ == '__main__':
    unittest.main()
