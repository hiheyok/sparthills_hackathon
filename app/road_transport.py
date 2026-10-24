"""Fetch road data with Python, with a native HTTPS fallback on Windows.

Python uses OpenSSL. Windows PowerShell uses the Windows/.NET networking stack,
which can resolve system proxy settings and certificate chains differently.
Both transports verify HTTPS certificates; neither receives browser credentials.
"""
import base64
import json
import os
import ssl
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

IS_WINDOWS = sys.platform == "win32"
MAX_RESPONSE_BYTES = 3_000_000

# Only this fixed script is executed. URL and headers arrive as JSON data over
# stdin, never as PowerShell code. Basic parsing avoids executing web content.
_WINDOWS_REQUEST = r"""
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
try {
    $request = [Console]::In.ReadToEnd() | ConvertFrom-Json
    [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
    $response = Invoke-WebRequest -UseBasicParsing -Uri $request.url -UserAgent $request.user_agent -Headers @{Accept='application/json'} -TimeoutSec ([int]$request.timeout)
    $bytes = [Text.Encoding]::UTF8.GetBytes([string]$response.Content)
    if ($bytes.Length -gt 3000000) { throw 'Response too large' }
    [Console]::Out.Write([Convert]::ToBase64String($bytes))
} catch {
    # Do not write URLs, proxy credentials or response bodies to logs.
    [Console]::Error.Write('Windows road-data request failed')
    exit 1
}
"""


def _windows_fetch(url, timeout, user_agent):
    encoded = base64.b64encode(_WINDOWS_REQUEST.encode("utf-16-le")).decode("ascii")
    result = subprocess.run(
        ["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
        input=json.dumps({"url": url, "timeout": timeout, "user_agent": user_agent}).encode("ascii"),
        capture_output=True, timeout=timeout + 2, check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if result.returncode:
        raise OSError("Windows road-data request failed")
    raw = base64.b64decode(result.stdout.strip(), validate=True)
    if len(raw) > MAX_RESPONSE_BYTES:
        raise ValueError("Road-data response too large")
    return raw


def _ssl_context():
    context = ssl.create_default_context()
    # python.org installs on macOS can lack their optional OpenSSL CA bundle.
    # Use the OS certificate bundle only when no default roots or custom bundle exist.
    system_bundle = Path("/etc/ssl/cert.pem")
    if (sys.platform == "darwin" and not os.environ.get("SSL_CERT_FILE")
            and not os.environ.get("SSL_CERT_DIR") and not context.get_ca_certs()
            and system_bundle.is_file()):
        context.load_verify_locations(cafile=str(system_bundle))
    return context


def fetch(url, timeout, user_agent):
    req = urllib.request.Request(url, headers={"User-Agent": user_agent, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_ssl_context()) as response:
            return response.read(MAX_RESPONSE_BYTES)
    except urllib.error.HTTPError:
        # An HTTP rejection came from the service; changing TLS stacks cannot fix it.
        raise
    except (urllib.error.URLError, OSError) as original:
        if not IS_WINDOWS:
            raise
        try:
            return _windows_fetch(url, timeout, user_agent)
        except (OSError, ValueError, subprocess.TimeoutExpired) as native:
            # Keep the original error category for the API's actionable diagnosis.
            raise original from native
