"""Local road-routing configuration and its diagnostic command."""
import contextlib
import io
import os
import sys
import tempfile
from pathlib import Path
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'app'))
with mock.patch.dict(os.environ):
    import local_server
import roads


class LocalRoutingTests(unittest.TestCase):
    def test_map_settings_load_without_overriding_exported_credentials(self):
        with tempfile.TemporaryDirectory() as folder, mock.patch.dict(os.environ, {}, clear=True), \
                mock.patch.object(local_server, 'ROOT', Path(folder)):
            Path(folder, '.env.local').write_text('GOOGLE_MAPS_API_KEY=local-test-key\nGOOGLE_MAPS_MAP_ID=demo-map\nUNRELATED_SETTING=ignored\n')
            local_server.load_local_settings()
            self.assertEqual(os.environ['GOOGLE_MAPS_API_KEY'], 'local-test-key')
            self.assertEqual(os.environ['GOOGLE_MAPS_MAP_ID'], 'demo-map')
            self.assertNotIn('UNRELATED_SETTING', os.environ)
            os.environ['GOOGLE_MAPS_API_KEY'] = 'exported-key'
            local_server.load_local_settings()
            self.assertEqual(os.environ['GOOGLE_MAPS_API_KEY'], 'exported-key')

    def test_explicit_roads_on_overrides_environment_without_starting_server(self):
        output = io.StringIO()
        with mock.patch.dict(os.environ, {'ROAD_ROUTING': 'off'}), \
                mock.patch.object(sys, 'argv', ['local_server.py', '--roads', 'on', '--check-routing']), \
                mock.patch.object(roads, 'fetch_route', return_value={'path': [[37, -121], [37.1, -121.1]], 'distance_km': 2}) as fetch, \
                mock.patch.object(local_server, 'ThreadingHTTPServer') as server, contextlib.redirect_stdout(output):
            self.assertEqual(local_server.main(), 0)
            self.assertEqual(os.environ['ROAD_ROUTING'], 'on')
        fetch.assert_called_once()
        server.assert_not_called()
        self.assertIn('Road routing OK', output.getvalue())

    def test_failed_check_returns_failure_and_actionable_reason(self):
        output = io.StringIO()
        with mock.patch.dict(os.environ, {'ROAD_ROUTING': 'off'}), \
                mock.patch.object(sys, 'argv', ['local_server.py', '--check-routing']), \
                mock.patch.object(local_server, 'ThreadingHTTPServer') as server, contextlib.redirect_stderr(output):
            self.assertEqual(local_server.main(), 1)
        server.assert_not_called()
        self.assertIn('(disabled)', output.getvalue())
        self.assertIn('ROAD_ROUTING=on', output.getvalue())


if __name__ == '__main__':
    unittest.main()
