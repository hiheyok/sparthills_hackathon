#!/usr/bin/env python3
"""End-to-end check that Amazon Bedrock is really answering inside the app.

    python3 scripts/smoke.py https://abc123.execute-api.us-east-1.amazonaws.com   # deployed app
    python3 scripts/smoke.py                                                      # local code, your AWS credentials

Runs the full pipeline (understand -> search -> alternatives -> brief) and prints
which model handled each stage. Exits 1 if any stage fell back to the rules
engine, so you know before you record the demo video. Use --allow-fallback to
accept fallbacks.
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

TEXT = ("Wildfire warning and the power is out. My dad uses insulin and it has to stay cold. "
        "We also have a 6-month-old who needs formula and diapers.")
BODY = {"text": TEXT, "lat": 37.3382, "lng": -121.8863, "radius_km": 8, "local_minutes": 10 * 60}


def run_remote(url):
    req = urllib.request.Request(url.rstrip("/") + "/api/plan", data=json.dumps(BODY).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        sys.exit(f"HTTP {e.code}: {e.read().decode()[:400]}")


def run_local():
    sys.path.insert(0, os.path.join(ROOT, "app"))
    os.environ["AI_MODE"] = "bedrock"
    import handler
    event = {"rawPath": "/api/plan", "requestContext": {"http": {"method": "POST", "sourceIp": "smoke"}},
             "body": json.dumps(BODY), "isBase64Encoded": False}
    r = handler.handler(event, None)
    body = json.loads(r["body"])
    if r["statusCode"] != 200:
        sys.exit(f"HTTP {r['statusCode']}: {body}")
    return body


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("url", nargs="?")
    ap.add_argument("--allow-fallback", action="store_true")
    args = ap.parse_args()

    out = run_remote(args.url) if args.url else run_local()
    if not isinstance(out, dict) or "traces" not in out or "plan" not in out or "brief" not in out:
        sys.exit(f"Unexpected response from /api/plan (no plan/brief/traces): {str(out)[:400]}")
    print(f"Request: {TEXT}\n")
    print(f"{'stage':<14}{'handled by':<10}{'model':<48}{'time':>7}  tokens in/out")
    bad = 0
    for t in out["traces"]:
        model = t.get("model") or "-"
        print(f"{t['stage']:<14}{t['mode']:<10}{model:<48}{t['ms'] / 1000:>6.1f}s  {t['tokens_in']}/{t['tokens_out']}")
        if t["mode"] != "bedrock":
            bad += 1
            print(f"   fell back: {t.get('reason')}")
            for tried in t.get("tried", []):
                print(f"   - {tried.get('model')}: {tried.get('error')}")
    plan = out["plan"]
    print(f"\nPlan: {out['brief']['headline']}")
    for s in plan["route"]["stops"]:
        print(f"  {s['order']}. {s['store']['name']} ({s['leg_min']} min): " + ", ".join(i["name"] for i in s["items"]))
    print(f"\n{len(out['traces']) - bad} of {len(out['traces'])} stages ran on Amazon Bedrock.")
    if bad and not args.allow_fallback:
        print("FAIL: at least one stage used the rules fallback. See the reasons above "
              "(common: model access not enabled, region blocked, no credentials).")
        sys.exit(1)
    print("OK")


if __name__ == "__main__":
    main()
