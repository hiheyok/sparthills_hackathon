#!/usr/bin/env python3
"""Run SupplyLine locally with no dependencies beyond Python 3.9+.

    python3 local_server.py                  # AI = Bedrock if AWS credentials exist, else rules fallback
    python3 local_server.py --ai off         # force the rules-only fallback (no AWS calls)
    python3 local_server.py --ai bedrock     # always try Bedrock first
    python3 local_server.py --port 9000

For real Bedrock locally, export AWS credentials first (the event page has a
"Get AWS CLI credentials" button), then: pip install boto3
"""
import argparse
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "app"))
os.environ.setdefault("REPORTS_FILE", str(ROOT / "local_data" / "reports.json"))


class Local(BaseHTTPRequestHandler):
    def _run(self, method):
        import handler
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length).decode("utf-8", "replace") if length else ""
        event = {"rawPath": urlsplit(self.path).path,
                 "requestContext": {"http": {"method": method, "sourceIp": self.client_address[0]}},
                 "body": body, "isBase64Encoded": False}
        r = handler.handler(event, None)
        payload = r["body"]
        data = __import__("base64").b64decode(payload) if r.get("isBase64Encoded") else payload.encode("utf-8")
        self.send_response(r["statusCode"])
        for k, v in r["headers"].items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self._run("GET")

    def do_POST(self):
        self._run("POST")

    def log_message(self, fmt, *args):
        sys.stderr.write("  %s\n" % (fmt % args))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--ai", choices=["auto", "bedrock", "off"], default=os.environ.get("AI_MODE", "auto"))
    args = ap.parse_args()
    os.environ["AI_MODE"] = args.ai
    import ai
    st = ai.status()
    print(f"SupplyLine on http://localhost:{args.port}")
    print(f"  AI mode: {st['mode']}   region: {st['region']}")
    print("  models:  " + ", ".join(st["models"]))
    if args.ai == "off":
        print("  (rules-only fallback: no AWS calls will be made)")
    ThreadingHTTPServer(("127.0.0.1", args.port), Local).serve_forever()


if __name__ == "__main__":
    main()
