"""Windows native fallback and transport failure handling."""
import base64
import json
import os
import ssl
import subprocess
import sys
import threading
import tempfile
import unittest
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
import road_transport

URL = 'https://router.project-osrm.org/route/v1/driving/-121,37;-122,38?overview=full'
RAW = b'{"code":"Ok","routes":[]}'
CERT_ERROR = urllib.error.URLError(ssl.SSLCertVerificationError(1, 'certificate verify failed'))


class TransportTests(unittest.TestCase):
    def test_macos_without_python_roots_uses_system_bundle_with_verification(self):
        with mock.patch('road_transport.ssl.create_default_context') as create, \
                mock.patch('road_transport.sys.platform', 'darwin'), \
                mock.patch.dict(os.environ, {}, clear=True), \
                mock.patch('road_transport.Path.is_file', return_value=True):
            context = create.return_value
            context.get_ca_certs.return_value = []
            self.assertIs(road_transport._ssl_context(), context)
            context.load_verify_locations.assert_called_once_with(cafile='/etc/ssl/cert.pem')

    def test_custom_certificate_bundle_is_preserved(self):
        with mock.patch('road_transport.ssl.create_default_context') as create, \
                mock.patch('road_transport.sys.platform', 'darwin'), \
                mock.patch.dict(os.environ, {'SSL_CERT_FILE': '/custom/trusted.pem'}):
            context = create.return_value
            context.get_ca_certs.return_value = []
            road_transport._ssl_context()
            context.load_verify_locations.assert_not_called()

    def test_windows_recovers_from_python_certificate_failure(self):
        with mock.patch.object(road_transport, 'IS_WINDOWS', True), \
                mock.patch('road_transport.urllib.request.urlopen', side_effect=CERT_ERROR), \
                mock.patch.object(road_transport, '_windows_fetch', return_value=RAW) as native:
            self.assertEqual(road_transport.fetch(URL, 6, 'SupplyLine/test'), RAW)
        native.assert_called_once_with(URL, 6, 'SupplyLine/test')

    def test_other_platforms_do_not_launch_powershell(self):
        with mock.patch.object(road_transport, 'IS_WINDOWS', False), \
                mock.patch('road_transport.urllib.request.urlopen', side_effect=CERT_ERROR), \
                mock.patch.object(road_transport, '_windows_fetch') as native:
            with self.assertRaises(urllib.error.URLError):
                road_transport.fetch(URL, 6, 'SupplyLine/test')
        native.assert_not_called()

    def test_http_rejections_do_not_switch_transports(self):
        rejected = urllib.error.HTTPError(URL, 403, 'Forbidden', {}, None)
        with mock.patch.object(road_transport, 'IS_WINDOWS', True), \
                mock.patch('road_transport.urllib.request.urlopen', side_effect=rejected), \
                mock.patch.object(road_transport, '_windows_fetch') as native:
            with self.assertRaises(urllib.error.HTTPError):
                road_transport.fetch(URL, 6, 'SupplyLine/test')
        native.assert_not_called()
        rejected.close()

    def test_failed_native_fallback_preserves_certificate_diagnosis(self):
        for failure in [FileNotFoundError(), subprocess.TimeoutExpired('powershell.exe', 8), OSError('blocked')]:
            with self.subTest(failure=type(failure).__name__), \
                    mock.patch.object(road_transport, 'IS_WINDOWS', True), \
                    mock.patch('road_transport.urllib.request.urlopen', side_effect=CERT_ERROR), \
                    mock.patch.object(road_transport, '_windows_fetch', side_effect=failure):
                with self.assertRaises(urllib.error.URLError) as caught:
                    road_transport.fetch(URL, 6, 'SupplyLine/test')
                self.assertIs(caught.exception, CERT_ERROR)

    def test_native_request_passes_url_as_data_and_has_process_timeout(self):
        url = URL + '&x=$(throw "must not execute")'
        result = subprocess.CompletedProcess([], 0, base64.b64encode(RAW), b'')
        with mock.patch('road_transport.subprocess.run', return_value=result) as run:
            self.assertEqual(road_transport._windows_fetch(url, 6, 'SupplyLine/test'), RAW)
        args, kwargs = run.call_args
        self.assertNotIn(url, ' '.join(args[0]))
        self.assertEqual(json.loads(kwargs['input'])['url'], url)
        self.assertEqual(kwargs['timeout'], 8)
        self.assertFalse(kwargs.get('shell', False))

    def test_native_failures_and_bad_output_are_not_used_as_geometry(self):
        for result in [subprocess.CompletedProcess([], 1, b'', b'private details'),
                       subprocess.CompletedProcess([], 0, b'not base64!', b'')]:
            with mock.patch('road_transport.subprocess.run', return_value=result):
                with self.assertRaises((OSError, ValueError)):
                    road_transport._windows_fetch(URL, 6, 'SupplyLine/test')


@unittest.skipUnless(sys.platform == 'win32', 'native PowerShell integration requires Windows')
class WindowsIntegration(unittest.TestCase):
    def test_native_https_rejects_an_untrusted_certificate(self):
        # A real TLS server verifies that the native fallback never skips validation.
        import datetime
        import ipaddress
        from pathlib import Path
        try:
            from cryptography import x509
            from cryptography.hazmat.primitives import hashes, serialization
            from cryptography.hazmat.primitives.asymmetric import rsa
            from cryptography.x509.oid import NameOID
        except ImportError:
            self.skipTest('cryptography (installed with moto) is required for the TLS fixture')
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'localhost')])
        now = datetime.datetime.now(datetime.timezone.utc)
        cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
                .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(days=1))
                .not_valid_after(now + datetime.timedelta(days=1))
                .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address('127.0.0.1'))]), critical=False)
                .sign(key, hashes.SHA256()))
        with tempfile.TemporaryDirectory() as tmp:
            cert_file, key_file = Path(tmp) / 'cert.pem', Path(tmp) / 'key.pem'
            cert_file.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
            key_file.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                                   serialization.NoEncryption()))
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.load_cert_chain(cert_file, key_file)
            server = ThreadingHTTPServer(('127.0.0.1', 0), BaseHTTPRequestHandler)
            server.socket = ctx.wrap_socket(server.socket, server_side=True)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                with self.assertRaises(OSError):
                    road_transport._windows_fetch(f'https://127.0.0.1:{server.server_port}/', 6, 'SupplyLine/test')
            finally:
                server.shutdown()
                server.server_close()
                thread.join()

    def test_real_native_request_recovers_when_python_transport_fails(self):
        requests = []

        class Reply(BaseHTTPRequestHandler):
            def do_GET(self):
                requests.append((self.path, self.headers.get('User-Agent'), self.headers.get('Accept')))
                self.send_response(200)
                self.send_header('Content-Type', 'application/json; charset=utf-8')
                self.send_header('Content-Length', str(len(RAW)))
                self.end_headers()
                self.wfile.write(RAW)

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(('127.0.0.1', 0), Reply)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = f'http://127.0.0.1:{server.server_port}/route?overview=full&geometries=geojson'
            with mock.patch('road_transport.urllib.request.urlopen', side_effect=CERT_ERROR):
                self.assertEqual(road_transport.fetch(url, 6, 'SupplyLine/test'), RAW)
            self.assertEqual(requests, [('/route?overview=full&geometries=geojson', 'SupplyLine/test', 'application/json')])
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == '__main__':
    unittest.main()
