"""End-to-end tests through the Lambda handler (same entry point as AWS)."""
import base64
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
os.environ["AI_MODE"] = "off"          # deterministic: rules fallback, no AWS calls
os.environ.pop("REPORTS_TABLE", None)
os.environ.pop("REPORTS_FILE", None)

import ai  # noqa: E402
import handler  # noqa: E402
import store  # noqa: E402

DOWNTOWN = {"lat": 37.3382, "lng": -121.8863, "radius_km": 8, "local_minutes": 600}


def call(method, path, body=None, ip="1.2.3.4"):
    event = {"rawPath": path, "requestContext": {"http": {"method": method, "sourceIp": ip}},
             "body": json.dumps(body) if body is not None else None}
    r = handler.handler(event, None)
    payload = base64.b64decode(r["body"]) if r.get("isBase64Encoded") else r["body"].encode("utf-8")
    return r["statusCode"], r["headers"], payload


def post(path, body, **kw):
    status, headers, payload = call("POST", path, body, **kw)
    return status, json.loads(payload)


class HandlerTests(unittest.TestCase):
    def setUp(self):
        store.set_store(store.LocalReportStore())
        handler._hits.clear()
        os.environ["AI_MODE"] = "off"

    def test_static_files_and_security_headers(self):
        status, headers, body = call("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(b"SupplyLine", body)
        self.assertIn("default-src 'self'", headers["Content-Security-Policy"])
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        for path, ctype in [("/static/app.js", "javascript"), ("/static/styles.css", "text/css"),
                            ("/sw.js", "javascript"), ("/manifest.webmanifest", "manifest"),
                            ("/static/vendor/leaflet.js", "javascript"), ("/static/icon.svg", "svg")]:
            status, headers, _ = call("GET", path)
            self.assertEqual(status, 200, path)
            self.assertIn(ctype, headers["Content-Type"], path)

    def test_map_tiles_are_allowed_and_get_a_referer(self):
        # OpenStreetMap blocks tile requests with no Referer, which leaves the map blank.
        _, headers, _ = call("GET", "/")
        self.assertNotEqual(headers["Referrer-Policy"], "no-referrer")
        self.assertIn(headers["Referrer-Policy"], {"strict-origin-when-cross-origin", "origin", "origin-when-cross-origin"})
        self.assertIn("https://tile.openstreetmap.org", headers["Content-Security-Policy"])

    def test_fonts_are_served_as_binary(self):
        status, headers, body = call("GET", "/static/fonts/overpass-latin-wght-normal.woff2")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "font/woff2")
        self.assertEqual(body[:4], b"wOF2")

    def test_path_traversal_and_unknown_paths_are_blocked(self):
        for path in ["/static/../handler.py", "/static/..%2Fhandler.py", "/static/%2e%2e/handler.py",
                     "/handler.py", "/static/data/stores.json/../../handler.py", "/nope"]:
            self.assertEqual(call("GET", path)[0], 404, path)
        self.assertEqual(call("DELETE", "/api/plan")[0], 405)

    def test_meta_and_health(self):
        status, _, body = call("GET", "/api/meta")
        meta = json.loads(body)
        self.assertEqual(status, 200)
        self.assertTrue(meta["places"] and meta["catalog"])
        self.assertTrue(meta["data"]["demo"])
        self.assertTrue(json.loads(call("GET", "/api/health")[2])["ok"])

    def test_validation_errors(self):
        self.assertEqual(post("/api/understand", {"text": "x"})[0], 400)
        self.assertEqual(post("/api/search", {"needs": [{"item_id": "water_bottled"}]})[0], 400)          # no location
        self.assertEqual(post("/api/search", {**DOWNTOWN, "lat": 999, "needs": [{"item_id": "water_bottled"}]})[0], 400)
        self.assertEqual(post("/api/search", {**DOWNTOWN, "needs": [{"item_id": "bogus"}]})[0], 400)
        self.assertEqual(post("/api/search", {**DOWNTOWN, "lat": "nan", "needs": [{"item_id": "water_bottled"}]})[0], 400)
        status, _, _ = call("POST", "/api/search", None)
        self.assertEqual(status, 400)
        event = {"rawPath": "/api/search", "requestContext": {"http": {"method": "POST"}}, "body": "{not json"}
        self.assertEqual(handler.handler(event, None)["statusCode"], 400)
        big = {"rawPath": "/api/search", "requestContext": {"http": {"method": "POST"}}, "body": "x" * 70000}
        self.assertEqual(handler.handler(big, None)["statusCode"], 413)

    def test_unintelligible_request_returns_422_with_guidance(self):
        status, body = post("/api/understand", {"text": "hello there how are you"})
        self.assertEqual(status, 422)
        self.assertIn("naming a few items", body["error"])

    def test_staged_flow_matches_what_the_ui_does(self):
        text = "Wildfire warning, power is out. My dad uses insulin. We have a baby who needs formula and diapers."
        status, u = post("/api/understand", {"text": text})
        self.assertEqual(status, 200)
        ids = {n["item_id"] for n in u["intent"]["needs"]}
        self.assertTrue({"insulin", "cooler", "ice_bag", "baby_formula_powder", "diapers"} <= ids)
        status, s = post("/api/search", {**DOWNTOWN, "needs": u["intent"]["needs"]})
        self.assertEqual(status, 200)
        self.assertIn("baby_formula_powder", s["gaps"])
        status, a = post("/api/alternatives", {**DOWNTOWN, "intent": u["intent"]})
        self.assertEqual(status, 200)
        formula = next(x for x in a["alternatives"] if x["for_item"] == "baby_formula_powder")
        self.assertIn("baby_formula_rtf", [o["item_id"] for o in formula["options"]])
        status, b = post("/api/brief", {**DOWNTOWN, "intent": u["intent"], "alternatives": a["alternatives"]})
        self.assertEqual(status, 200)
        stops = b["plan"]["route"]["stops"]
        self.assertTrue(stops)
        swapped = [i for s_ in stops for i in s_["items"] if i["is_alt"]]
        self.assertTrue(any(i["item_id"] == "baby_formula_rtf" for i in swapped))
        self.assertIn("instead of", b["plan_text"])
        self.assertTrue(b["brief"]["headline"])

    def test_tampered_alternatives_cannot_substitute_prescriptions(self):
        reports = {}
        import engine
        for s in engine.STORES:
            if "insulin" in s["inv"]:
                store.get_store().put(s["id"], "insulin", "out")
        intent = {"needs": [{"item_id": "insulin", "priority": 1, "qty": 1, "label": "insulin"}]}
        evil = [{"for_item": "insulin", "verdict": "substitutes_available", "escalate": "",
                 "options": [{"item_id": "glucose_tabs", "fit": "strong", "why": "same", "how_to_use": "", "caution": ""}]}]
        status, b = post("/api/brief", {**DOWNTOWN, "intent": intent, "alternatives": evil})
        self.assertEqual(status, 200)
        alt = b["plan"]["alternatives"][0]
        self.assertEqual(alt["options"], [])
        self.assertEqual(alt["verdict"], "no_safe_substitute")
        self.assertEqual(b["plan"]["route"]["stops"], [])
        self.assertIn("pharmacy", alt["escalate"])

    def test_full_plan_endpoint_and_urgent_banner(self):
        status, body = post("/api/plan", {**DOWNTOWN, "text": "Earthquake. We smell gas in the garage and have no water. Need water and canned food."})
        self.assertEqual(status, 200)
        self.assertTrue(body["urgent"])
        self.assertIn("911", body["plan_text"])
        self.assertEqual(len(body["traces"]), 3)
        self.assertTrue(all(t["mode"] == "rules" for t in body["traces"]))

    def test_report_flow_changes_results(self):
        needs = [{"item_id": "insulin", "priority": 1, "qty": 1, "label": "insulin"}]
        _, before = post("/api/search", {**DOWNTOWN, "needs": needs})
        top = before["results"][0]["stores"][0]["id"]
        self.assertEqual(post("/api/report", {"store_id": top, "item_id": "insulin", "status": "out"})[0], 200)
        _, after = post("/api/search", {**DOWNTOWN, "needs": needs})
        self.assertNotIn(top, [s["id"] for s in after["results"][0]["stores"]])
        self.assertEqual(post("/api/report", {"store_id": top, "item_id": "*", "status": "closed"})[0], 200)

    def test_report_validation(self):
        bad = [{"store_id": "nope", "item_id": "insulin", "status": "out"},
               {"store_id": "s01", "item_id": "nope", "status": "out"},
               {"store_id": "s01", "item_id": "insulin", "status": "exploded"},
               {"store_id": "s01", "item_id": "insulin", "status": "closed"},
               {"store_id": "s01", "item_id": "*", "status": "out"}]
        for b in bad:
            self.assertEqual(post("/api/report", b)[0], 400, b)

    def test_rate_limit(self):
        codes = [post("/api/followup", {"question": "hello?", "plan_text": "x"}, ip="9.9.9.9")[0] for _ in range(95)]
        self.assertEqual(codes[0], 200)
        self.assertIn(429, codes)

    def test_followup(self):
        status, body = post("/api/followup", {"question": "Can I bring the dog?", "plan_text": "PLAN"})
        self.assertEqual(status, 200)
        self.assertEqual(body["trace"]["mode"], "rules")
        self.assertEqual(post("/api/followup", {"question": "?"})[0], 400)


if __name__ == "__main__":
    unittest.main()
