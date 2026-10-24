import os
import sys
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import engine as e  # noqa: E402

DOWNTOWN = (37.3382, -121.8863)
ALMADEN = (37.2260, -121.8640)
MORNING = 10 * 60


def need(item_id, priority=2, qty=1):
    return {"item_id": item_id, "priority": priority, "qty": qty, "label": e.CATALOG[item_id]["name"]}


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.now = time.time()

    def search(self, item, loc=DOWNTOWN, radius=8, minute=MORNING, reports=None, qty=1):
        return e.search_item(item, qty, loc[0], loc[1], radius, minute, reports or {}, self.now)

    def test_catalog_and_stores_are_consistent(self):
        for s in e.STORES:
            for item in s["inv"]:
                self.assertIn(item, e.CATALOG, f"{s['id']} stocks unknown item {item}")
        for c in e.CATALOG_LIST:
            self.assertIn(c["policy"], ("free", "caution", "none"))
            if c["policy"] == "none":
                self.assertTrue(c["escalate"], f"{c['id']} needs escalation guidance")

    def test_shortage_zone_creates_gaps_downtown_but_not_in_almaden(self):
        for item in ("water_bottled", "batt_d", "propane_1lb", "baby_formula_powder"):
            self.assertEqual(self.search(item)["status"], "gap", item)
        self.assertNotEqual(self.search("water_bottled", ALMADEN)["status"], "gap")

    def test_gap_reports_nearest_available_store_elsewhere(self):
        near = e.nearest_available("water_bottled", DOWNTOWN[0], DOWNTOWN[1], MORNING, {}, self.now)
        self.assertIsNotNone(near)
        self.assertGreater(near["dist_km"], 8)

    def test_open_state_boundaries(self):
        store = {"status": "open", "note": "", "hours": {"open": 8 * 60, "close": 21 * 60}}
        self.assertFalse(e.open_state(store, 7 * 60 + 59)[0])
        self.assertTrue(e.open_state(store, 8 * 60)[0])
        self.assertTrue(e.open_state(store, 20 * 60 + 45)[0])
        self.assertFalse(e.open_state(store, 20 * 60 + 46)[0])  # 15 minute closing buffer
        self.assertTrue(e.open_state({**store, "hours": {"always": True}}, 3 * 60)[0])
        self.assertFalse(e.open_state({**store, "status": "closed", "note": "Closed: power outage"}, MORNING)[0])
        self.assertFalse(e.open_state(store, MORNING, reported_closed=True)[0])
        # arrival after midnight for a store that closes at 24:00
        late = {"status": "open", "note": "", "hours": {"open": 6 * 60, "close": 24 * 60}}
        self.assertFalse(e.open_state(late, 24 * 60 + 10)[0])

    def test_closed_at_night_removes_daytime_only_stores(self):
        day = self.search("cooler", minute=MORNING)
        night = self.search("cooler", minute=23 * 60)
        self.assertGreater(day["total_matches"], night["total_matches"])

    def test_neighbor_report_out_removes_store(self):
        base = self.search("insulin")
        self.assertEqual(base["status"], "found")
        top = base["stores"][0]["id"]
        rep = {(top, "insulin"): {"status": "out", "ts": self.now - 30}}
        after = self.search("insulin", reports=rep)
        self.assertNotIn(top, [s["id"] for s in after["stores"]])

    def test_newer_report_overrides_older_snapshot_and_older_report_does_not(self):
        gap = self.search("water_bottled")
        self.assertEqual(gap["status"], "gap")
        sid = next(s["id"] for s in e.STORES if "water_bottled" in s["inv"] and s["status"] != "closed"
                   and s["hours"].get("always"))
        fresh = {(sid, "water_bottled"): {"status": "in_stock", "ts": self.now - 60}}
        found = e.search_item("water_bottled", 1, e.STORE_BY_ID[sid]["lat"], e.STORE_BY_ID[sid]["lng"], 1.0,
                              MORNING, fresh, self.now)
        self.assertEqual(found["status"], "found")
        self.assertEqual(found["stores"][0]["source"], "community")
        # a report older than the snapshot must not override it
        age = e.STORE_BY_ID[sid]["inv"]["water_bottled"]["age"]
        stale = {(sid, "water_bottled"): {"status": "in_stock", "ts": self.now - (age + 600) * 60}}
        res = e.stock_of(e.STORE_BY_ID[sid], "water_bottled", stale, self.now)
        self.assertEqual(res[2], "snapshot")

    def test_store_closed_report(self):
        top = self.search("insulin")["stores"][0]["id"]
        rep = {(top, "*"): {"status": "closed", "ts": self.now}}
        self.assertNotIn(top, [s["id"] for s in self.search("insulin", reports=rep)["stores"]])

    def test_partial_when_quantity_short(self):
        res = e.search_item("insulin", 999, DOWNTOWN[0], DOWNTOWN[1], 8, MORNING, {}, self.now)
        self.assertEqual(res["status"], "partial")

    def test_route_is_open_on_arrival_and_covers_found_items(self):
        needs = [need(i) for i in ("insulin", "cooler", "diapers", "food_canned")]
        plan = e.build_plan(needs, [], *DOWNTOWN, 8, MORNING, {}, self.now)
        self.assertLessEqual(len(plan["route"]["stops"]), 4)
        placed = {i["item_id"] for s in plan["route"]["stops"] for i in s["items"]}
        self.assertEqual(placed, {"insulin", "cooler", "diapers", "food_canned"})
        self.assertEqual(plan["unresolved"], [])
        arrivals = [s["arrive"] for s in plan["route"]["stops"]]
        self.assertEqual(len(arrivals), len(plan["route"]["stops"]))

    def test_alternative_is_swapped_into_route_and_marked(self):
        alts = [{"for_item": "water_bottled", "verdict": "substitutes_available", "no_purchase_tips": [],
                 "escalate": None,
                 "options": [{"item_id": "water_filter", "fit": "strong", "why": "x", "how_to_use": "y", "caution": ""}]}]
        plan = e.build_plan([need("water_bottled", 1)], alts, *DOWNTOWN, 8, MORNING, {}, self.now)
        items = [i for s in plan["route"]["stops"] for i in s["items"]]
        self.assertEqual(len(items), 1)
        self.assertTrue(items[0]["is_alt"])
        self.assertEqual(items[0]["replaces"], "water_bottled")
        self.assertEqual(plan["summary"]["swapped"], 1)
        self.assertEqual(plan["alternatives"][0]["options"][0]["status"], "found")

    def test_policy_none_items_are_never_substituted(self):
        reports = {(s["id"], "insulin"): {"status": "out", "ts": self.now} for s in e.STORES if "insulin" in s["inv"]}
        alts = [{"for_item": "insulin", "verdict": "substitutes_available", "no_purchase_tips": [], "escalate": None,
                 "options": [{"item_id": "glucose_tabs", "fit": "strong", "why": "", "how_to_use": "", "caution": ""}]}]
        plan = e.build_plan([need("insulin", 1)], alts, *DOWNTOWN, 8, MORNING, reports, self.now)
        self.assertEqual(plan["alternatives"][0]["options"], [])
        self.assertEqual(plan["alternatives"][0]["verdict"], "no_safe_substitute")
        self.assertTrue(plan["alternatives"][0]["escalate"])
        self.assertEqual(plan["route"]["stops"], [])
        self.assertEqual(plan["summary"]["unresolved"], 1)

    def test_caution_item_rejects_option_outside_allowlist_even_if_in_stock(self):
        alts = [{"for_item": "baby_formula_powder", "verdict": "substitutes_available", "no_purchase_tips": [], "escalate": None,
                 "options": [{"item_id": "baby_food", "fit": "strong", "why": "", "how_to_use": "", "caution": ""},
                             {"item_id": "baby_formula_rtf", "fit": "strong", "why": "", "how_to_use": "", "caution": ""}]}]
        plan = e.build_plan([need("baby_formula_powder", 1)], alts, *DOWNTOWN, 8, MORNING, {}, self.now)
        self.assertEqual([o["item_id"] for o in plan["alternatives"][0]["options"]], ["baby_formula_rtf"])
        items = [i for s in plan["route"]["stops"] for i in s["items"]]
        self.assertEqual([i["item_id"] for i in items], ["baby_formula_rtf"])

    def test_critical_items_are_not_left_for_last(self):
        needs = [need("insulin", 1), need("baby_formula_powder", 1), need("cooler"), need("diapers"),
                 need("ice_bag"), need("n95_mask")]
        alts = [{"for_item": "baby_formula_powder", "verdict": "x", "no_purchase_tips": [], "escalate": None,
                 "options": [{"item_id": "baby_formula_rtf", "fit": "strong", "why": "", "how_to_use": "", "caution": ""}]}]
        plan = e.build_plan(needs, alts, *DOWNTOWN, 8, MORNING, {}, self.now)
        first = plan["route"]["stops"][0]
        self.assertTrue(any(i["priority"] == 1 for i in first["items"]), "first stop should hold a critical item")

    def test_plan_text_mentions_swaps_and_escalation(self):
        alts = [{"for_item": "baby_formula_powder", "verdict": "substitutes_available", "no_purchase_tips": [],
                 "escalate": None,
                 "options": [{"item_id": "baby_formula_rtf", "fit": "strong", "why": "", "how_to_use": "", "caution": "Ask pediatrician"}]}]
        plan = e.build_plan([need("baby_formula_powder", 1)], alts, *DOWNTOWN, 8, MORNING, {}, self.now)
        text = e.render_plan_text(plan, {"headline": "Go now", "safety_notes": ["Stay safe"]}, urgent=True)
        self.assertIn("call 911", text)
        self.assertIn("instead of Infant formula (powder)", text)


if __name__ == "__main__":
    unittest.main()
