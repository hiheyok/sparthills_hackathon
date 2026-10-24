"""End-to-end tests through the Lambda handler (same entry point as AWS)."""
import base64
import json
import os
import sys
import unittest
import unittest.mock

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

    def test_unversioned_app_assets_are_revalidated(self):
        for path in ('/static/app.js', '/static/styles.css'):
            _, headers, _ = call('GET', path)
            self.assertEqual(headers['Cache-Control'], 'no-cache')

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

    def test_rules_fallback_does_not_swap_unrelated_gear_for_missing_batteries(self):
        status, body = post("/api/plan", {**DOWNTOWN, "text": "D batteries for my lantern"})
        self.assertEqual(status, 200)
        alt = next(a for a in body["plan"]["alternatives"] if a["for_item"] == "batt_d")
        self.assertEqual((alt["options"], alt["verdict"]), ([], "none_found"))
        self.assertEqual([i for s in body["plan"]["route"]["stops"] for i in s["items"] if i["is_alt"]], [])
        self.assertIn("batt_d", [u["item_id"] for u in body["plan"]["unresolved"]])
        self.assertNotIn("Everything on your list is covered", body["brief"]["summary"])

    def test_oversized_request_is_never_described_as_covered(self):
        intent = {"needs": [{"item_id": "insulin", "priority": 1, "qty": 99, "label": "insulin"}]}
        status, b = post("/api/brief", {**DOWNTOWN, "intent": intent})
        self.assertEqual(status, 200)
        self.assertEqual(b["plan"]["summary"]["covered"], 0)
        self.assertEqual(len(b["plan"]["shortfalls"]), 1)
        self.assertNotIn("Everything on your list is covered", b["brief"]["summary"])
        self.assertIn("Not enough in stock", b["brief"]["summary"])
        self.assertIn("Not enough in stock", b["plan_text"])

    def test_multi_unit_formula_substitute_stays_unresolved_through_both_stages(self):
        for include_insulin in (False, True):
            with self.subTest(include_insulin=include_insulin):
                needs = [{"item_id": "baby_formula_powder", "priority": 1, "qty": 4}]
                if include_insulin:
                    needs.append({"item_id": "insulin", "priority": 1, "qty": 1})
                intent = {"needs": needs}
                status, a = post("/api/alternatives", {**DOWNTOWN, "intent": intent})
                self.assertEqual(status, 200)
                self.assertIn("You requested 4 units", a["alternatives"][0]["quantity_note"])
                status, b = post("/api/brief", {**DOWNTOWN, "intent": intent, "alternatives": a["alternatives"]})
                self.assertEqual(status, 200)
                self.assertEqual((b["plan"]["summary"]["covered"], b["plan"]["summary"]["unresolved"]),
                                 (int(include_insulin), 1))
                self.assertFalse(any(i["is_alt"] for s in b["plan"]["route"]["stops"] for i in s["items"]))
                self.assertIn("You requested 4 units", b["brief"]["summary"])
                self.assertNotIn("Everything on your list is covered", b["brief"]["summary"])
                self.assertIn("Quantity not confirmed:", b["plan_text"])
                prompt = ai.plan_for_prompt(b["plan"], intent)
                self.assertEqual(prompt["quantity_unconfirmed"][0]["requested"], 4)
                self.assertEqual(prompt["not_found_nearby"], [])

    def test_storm_example_never_pairs_a_propane_stove_with_butane(self):
        text = "A storm took out our power three days ago. We need D batteries for our lantern and propane for the camp stove."
        status, body = post("/api/plan", {**DOWNTOWN, "text": text})
        self.assertEqual(status, 200)
        items = [i for s in body["plan"]["route"]["stops"] for i in s["items"]]
        self.assertIn("camp_stove_propane", [i["item_id"] for i in items])
        self.assertEqual([i["item_id"] for i in items if i["replaces"] == "propane_1lb"], ["food_meals"])
        self.assertNotIn("Butane canister", body["plan_text"])
        import engine
        for s in engine.STORES:
            store.get_store().put(s["id"], "food_meals", "out")
        status, body = post("/api/plan", {**DOWNTOWN, "text": text})
        self.assertEqual(status, 200)
        self.assertIn("propane_1lb", [u["item_id"] for u in body["plan"]["unresolved"]])
        self.assertFalse(any(i["replaces"] == "propane_1lb" for s in body["plan"]["route"]["stops"] for i in s["items"]))

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
        self.assertFalse(body["grounded"])                              # no location or needs: just the plan text
        self.assertEqual(post("/api/followup", {"question": "?"})[0], 400)


class TravelModeApiTests(unittest.TestCase):
    NEEDS = [{"item_id": "insulin", "priority": 1, "qty": 1, "label": "insulin"},
             {"item_id": "diapers", "priority": 2, "qty": 1, "label": "diapers"}]

    def setUp(self):
        store.set_store(store.LocalReportStore())
        handler._hits.clear()
        os.environ["AI_MODE"] = "off"

    def test_every_stage_defaults_to_driving_and_accepts_walking(self):
        for path, body in (("/api/search", {"needs": self.NEEDS}), ("/api/alternatives", {"needs": self.NEEDS}),
                           ("/api/brief", {"intent": {"needs": self.NEEDS}})):
            status, drive = post(path, {**DOWNTOWN, **body})
            status_w, walk = post(path, {**DOWNTOWN, **body, "mode": "walking"})
            self.assertEqual((status, status_w), (200, 200), path)
            self.assertEqual((drive["mode"], walk["mode"]), ("driving", "walking"), path)

    def test_walking_search_reports_the_walking_range_and_times(self):
        _, drive = post("/api/search", {**DOWNTOWN, "needs": self.NEEDS})
        _, walk = post("/api/search", {**DOWNTOWN, "needs": self.NEEDS, "mode": "walking"})
        self.assertEqual((drive["meta"]["radius_km"], walk["meta"]["radius_km"]), (8, 3.0))
        d_eta = min(s["eta_min"] for s in drive["results"][0]["stores"])
        w_eta = min(s["eta_min"] for s in walk["results"][0]["stores"])
        self.assertGreater(w_eta, d_eta)

    def test_walking_brief_is_a_walking_plan_with_a_walking_briefing(self):
        status, b = post("/api/brief", {**DOWNTOWN, "intent": {"needs": self.NEEDS}, "mode": "walking"})
        self.assertEqual((status, b["plan"]["mode"], b["plan"]["route"]["mode"]), (200, "walking", "walking"))
        self.assertEqual((b["trace"]["lane"], b["trace"]["agent"]), ("walking", "brief"))
        self.assertIn("on foot", b["plan_text"])
        self.assertIn("on foot", b["brief"]["headline"] + b["brief"]["summary"])

    def test_bad_modes_are_refused_on_every_endpoint(self):
        for path in ("/api/search", "/api/alternatives", "/api/brief", "/api/route", "/api/followup", "/api/plan"):
            for bad in ("flying", ["walking"], 7, {"a": 1}):
                body = {**DOWNTOWN, "needs": self.NEEDS, "intent": {"needs": self.NEEDS}, "question": "Is it open?",
                        "text": "water", "points": [{"lat": 37.3, "lng": -121.9}, {"lat": 37.31, "lng": -121.9}], "mode": bad}
                self.assertEqual(post(path, body)[0], 400, (path, bad))

    def test_meta_tells_the_browser_what_it_can_ask_for(self):
        status, _, payload = call("GET", "/api/meta")
        meta = json.loads(payload)
        self.assertEqual(meta["travel"], {"modes": ["driving", "walking"], "walk_max_km": 3.0})
        self.assertEqual(set(meta["ai"]["agents"]), {"understand", "alternatives", "brief", "followup"})

    def test_both_lanes_in_one_call(self):
        status, out = post("/api/plan", {**DOWNTOWN, "text": "I need insulin and diapers", "modes": ["walking", "driving"]})
        self.assertEqual(status, 200)
        self.assertEqual(set(out["lanes"]), {"driving", "walking"})
        self.assertEqual(out["plan"]["mode"], "walking")              # the original keys follow the first mode listed
        self.assertEqual(out["lanes"]["walking"]["plan"]["mode"], "walking")
        self.assertEqual(out["lanes"]["driving"]["plan"]["mode"], "driving")
        brief_traces = [t for t in out["traces"] if t["stage"] == "brief"]
        self.assertEqual(sorted(t["lane"] for t in brief_traces), ["driving", "walking"])
        self.assertEqual(out["traces"][0]["stage"], "understand")
        status, single = post("/api/plan", {**DOWNTOWN, "text": "I need insulin and diapers"})
        self.assertNotIn("lanes", single)                             # the original response shape, untouched
        for bad in ([], ["driving", "walking", "driving", "walking"], "walking", ["x"]):
            self.assertEqual(post("/api/plan", {**DOWNTOWN, "text": "water", "modes": bad})[0], 400, bad)

    def test_the_assistant_is_grounded_in_both_plans_built_on_the_server(self):
        body = {**DOWNTOWN, "question": "Should I walk or drive?", "intent": {"needs": self.NEEDS}, "mode": "walking",
                "plan_text": "text the browser could have changed"}
        status, out = post("/api/followup", body)
        self.assertEqual(status, 200)
        self.assertTrue(out["grounded"])
        self.assertEqual(out["trace"]["mode"], "rules")               # no model in tests, so the rules answer
        self.assertIn("Driving:", out["answer"])
        self.assertIn("Walking (shown):", out["answer"])

    def test_the_assistant_sees_the_plans_the_engine_builds_not_whatever_the_browser_sends(self):
        seen = []
        real = ai.assistant

        def spy(question, situation=None, plan_text=""):
            seen.append(situation)
            return real(question, situation, plan_text)

        evil = {"diapers": [{"for_item": "insulin", "options": [{"item_id": "glucose_tabs"}]}]}   # not a list of lanes
        with unittest.mock.patch.object(ai, "assistant", spy):
            post("/api/followup", {**DOWNTOWN, "question": "What can I do?", "intent": {"needs": self.NEEDS},
                                   "alternatives": evil})
        (situation,) = seen
        self.assertEqual(situation["selected_mode"], "driving")
        self.assertEqual(set(situation["plans"]), {"driving", "walking"})
        self.assertEqual(situation["plans"]["walking"]["travel_mode"], "walking")
        self.assertEqual(situation["plans"]["driving"]["search_radius_km"], 8)
        self.assertEqual(situation["plans"]["walking"]["search_radius_km"], 3.0)
        self.assertEqual([n["item"] for n in situation["needs"]], ["Insulin (prescription)", "Diapers"])
        self.assertEqual(situation["local_time"], "10:00 AM")


if __name__ == "__main__":
    unittest.main()
