"""Real Qt HTTP checks for the separately negotiated shelf control namespace.

Loopback protocol test only; no OS clipboard, native windows or cloud service.
"""
from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
import time
import unittest

from PySide6.QtCore import QCoreApplication
from cloud_api import CloudApi

app = QCoreApplication.instance() or QCoreApplication([])


def until(condition, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if condition():
            return
        time.sleep(.002)
    raise AssertionError('Qt loopback response did not complete')


class ControlTransport(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.seen = []
        cls.responses = {}
        cls.held = threading.Event()
        cls.release = threading.Event()
        owner = cls

        class Handler(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'

            def log_message(self, *args):
                pass

            def do_GET(self):
                owner.seen.append((self.path, self.headers.get('Authorization')))
                status, body, hold = owner.responses.get(
                    self.path, (200, {'schema_version':1}, False))
                if hold:
                    owner.held.set()
                    if not owner.release.wait(5):
                        return
                data = json.dumps(body).encode('utf8')
                try:
                    self.send_response(status)
                    self.send_header('Content-Type', 'application/json')
                    self.send_header('Content-Length', str(len(data)))
                    self.send_header('Connection', 'close')
                    self.end_headers()
                    self.wfile.write(data)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    pass
                finally:
                    self.close_connection = True

        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        cls.server.daemon_threads = True
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.origin = 'http://127.0.0.1:' + str(cls.server.server_port)

    @classmethod
    def tearDownClass(cls):
        cls.release.set()
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(3)
        assert not cls.thread.is_alive()

    def setUp(self):
        self.api = CloudApi(self.origin, allow_test_http=True)
        self.api.token = 'disposable-local-control-token'
        self.seen.clear()
        self.responses.clear()
        self.held.clear()
        self.release.clear()

    def tearDown(self):
        self.release.set()
        self.api.close()
        app.processEvents()

    def request(self, path, *, status=200, body=None, **kwargs):
        self.responses[path] = (status, body if body is not None else {'schema_version':1}, False)
        result = []
        self.api.request('GET', path, lambda value, error: result.append((value, error)), **kwargs)
        until(lambda: result)
        self.assertEqual(len(result), 1)
        return result[0]

    def test_v2_shelf_control_uses_real_authenticated_transport(self):
        value, error = self.request('/v2/shelf/capabilities', body={
            'schema_version':2, 'shelf_peer_v1':True, 'replica_v1':True,
            'cursor_prefix':'shelf2:', 'encodings':['raw-v1']})
        self.assertIsNone(error)
        self.assertTrue(value['shelf_peer_v1'])
        self.assertEqual(self.seen, [('/v2/shelf/capabilities', 'Bearer ' + self.api.token)])

    def test_v1_response_contract_and_no_auth_health_remain_unchanged(self):
        value, error = self.request('/v1/health', authenticated=False)
        self.assertIsNone(error)
        self.assertEqual(value['schema_version'], 1)
        self.assertEqual(self.seen, [('/v1/health', None)])

    def test_each_namespace_requires_its_own_success_schema(self):
        for path, version in (('/v1/health', 2), ('/v2/shelf/capabilities', 1),
                              ('/v2/shelf/capabilities', True), ('/v2/shelf/capabilities', 2.0)):
            with self.subTest(path=path, version=version):
                value, error = self.request(path, body={'schema_version':version})
                self.assertIsNone(value)
                self.assertIsNotNone(error)
                self.assertEqual(error.status, 200)

    def test_other_v2_namespaces_remain_rejected_before_http(self):
        for path in ('/v2/account/me', '/v2/shelf', '/v3/shelf/capabilities',
                     'https://outside.invalid/v2/shelf/capabilities',
                     '/v2/shelf/../account/me', '/v2/shelf/%2e%2e/account/me',
                     '/v2/shelf/../../v1/health', '/v2/shelf/%2e./../v1/health',
                     '/v2/shelf/..%2faccount/me', '/v2/shelf/%2E%2E%5caccount/me',
                     '/v2/shelf/%252e%252e/account/me', '/v2/shelf/\\..\\account/me'):
            with self.subTest(path=path):
                value, error = self.request(path)
                self.assertIsNone(value)
                self.assertIsNotNone(error)
        self.assertEqual(self.seen, [])

    def test_shelf_query_cursor_remains_valid_and_is_not_treated_as_a_path(self):
        path='/v2/shelf/changes?cursor=shelf2%3A17&limit=100'
        value, error = self.request(path, body={'schema_version':2,'cursor':'shelf2:17','changes':[]})
        self.assertIsNone(error)
        self.assertEqual(value['cursor'], 'shelf2:17')
        self.assertEqual(self.seen, [(path, 'Bearer ' + self.api.token)])

    def test_real_disabled_capability_error_is_not_a_success_or_hidden(self):
        value, error = self.request('/v2/shelf/capabilities', status=404, body={
            'schema_version':2, 'error':{'code':'CAPABILITY_DISABLED', 'message':'Capability disabled'}})
        self.assertIsNone(value)
        self.assertIsNotNone(error)
        self.assertEqual((error.status, error.code), (404, 'CAPABILITY_DISABLED'))
        self.assertEqual(len(self.seen), 1)

    def test_missing_login_never_sends_control_request(self):
        self.api.token = ''
        value, error = self.request('/v2/shelf/capabilities', body={'schema_version':2})
        self.assertIsNone(value)
        self.assertEqual((error.status, error.code), (401, 'unauthorized'))
        self.assertEqual(self.seen, [])

    def test_cancelled_control_response_cannot_deliver_late_success(self):
        path = '/v2/shelf/snapshot'
        self.responses[path] = (200, {'schema_version':2, 'cursor':'shelf2:0'}, True)
        result = []
        key = self.api.request('GET', path, lambda value, error: result.append((value, error)))
        until(self.held.is_set)
        self.api.cancel(key)
        self.assertEqual(len(result), 1)
        self.assertIsNone(result[0][0])
        self.assertEqual(result[0][1].code, 'cancelled')
        self.release.set()
        deadline = time.monotonic() + .1
        while time.monotonic() < deadline:
            app.processEvents()
            time.sleep(.002)
        self.assertEqual(len(result), 1)


if __name__ == '__main__':
    unittest.main()
