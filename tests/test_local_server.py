"""Local road-routing configuration and its diagnostic command."""
import contextlib
import io
import json
import os
import socket
import sys
import threading
import unittest
from http.client import HTTPResponse
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'app'))
with mock.patch.dict(os.environ):
    import local_server
import roads


class LocalRoutingTests(unittest.TestCase):
    def test_explicit_roads_on_overrides_environment_without_starting_server(self):
        output = io.StringIO()
        with mock.patch.dict(os.environ, {'ROAD_ROUTING': 'off'}), \
                mock.patch.object(sys, 'argv', ['local_server.py', '--roads', 'on', '--check-routing']), \
                mock.patch.object(roads, 'fetch_route', return_value={'path': [[37, -121], [37.1, -121.1]], 'distance_km': 2}) as fetch, \
                mock.patch.object(local_server, 'LocalHTTPServer') as server, contextlib.redirect_stdout(output):
            self.assertEqual(local_server.main(), 0)
            self.assertEqual(os.environ['ROAD_ROUTING'], 'on')
        fetch.assert_called_once()
        server.assert_not_called()
        self.assertIn('Road routing OK', output.getvalue())

    def test_failed_check_returns_failure_and_actionable_reason(self):
        output = io.StringIO()
        with mock.patch.dict(os.environ, {'ROAD_ROUTING': 'off'}), \
                mock.patch.object(sys, 'argv', ['local_server.py', '--check-routing']), \
                mock.patch.object(local_server, 'LocalHTTPServer') as server, contextlib.redirect_stderr(output):
            self.assertEqual(local_server.main(), 1)
        server.assert_not_called()
        self.assertIn('(disabled)', output.getvalue())
        self.assertIn('ROAD_ROUTING=on', output.getvalue())


class LocalHTTPServerTests(unittest.TestCase):
    def test_browser_connection_burst_waits_until_server_can_accept(self):
        class QuietLocal(local_server.Local):
            def log_message(self, *args):
                pass

        with local_server.LocalHTTPServer(('127.0.0.1', 0), QuietLocal) as server, \
                contextlib.ExitStack() as sockets:
            # Hold the accept loop back to reproduce a busy runner. A browser
            # can open more than five sockets before Python is scheduled again.
            clients = [sockets.enter_context(socket.create_connection(server.server_address, timeout=2))
                       for _ in range(16)]
            for client in clients:
                client.sendall(b'GET /api/meta HTTP/1.0\r\nHost: localhost\r\n\r\n')
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                for client in clients:
                    with HTTPResponse(client) as response:
                        response.begin()
                        self.assertEqual(response.status, 200)
                        self.assertTrue(json.loads(response.read())['places'])
            finally:
                server.shutdown()
                thread.join()


if __name__ == '__main__':
    unittest.main()
