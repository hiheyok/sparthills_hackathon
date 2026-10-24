import contextlib
import os
import sys
import time
import unittest
import unittest.mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import engine as e  # noqa: E402
import store as report_store  # noqa: E402
from geo import WALK_MAX_KM, eta_min, haversine_km, reach_km  # noqa: E402

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
        # The panic-bought items still gap at downtown STORES. Water is the exception: relief water
        # points keep it, so a downtown water search now finds a nearby free source instead of nothing.
        for item in ("batt_d", "propane_1lb", "baby_formula_powder"):
            self.assertEqual(self.search(item)["status"], "gap", item)
        self.assertEqual(self.search("water_bottled")["status"], "found")   # covered by a relief water point
        self.assertTrue(any(s["free"] for s in self.search("water_bottled")["stores"]))
        self.assertNotEqual(self.search("baby_formula_powder", ALMADEN)["status"], "gap")

    def test_gap_reports_nearest_available_store_elsewhere(self):
        near = e.nearest_available("baby_formula_powder", DOWNTOWN[0], DOWNTOWN[1], MORNING, {}, self.now)
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
        gap = self.search("batt_d")
        self.assertEqual(gap["status"], "gap")
        sid = next(s["id"] for s in e.STORES if "batt_d" in s["inv"] and s["status"] != "closed"
                   and s["hours"].get("always"))
        fresh = {(sid, "batt_d"): {"status": "in_stock", "ts": self.now - 60}}
        found = e.search_item("batt_d", 1, e.STORE_BY_ID[sid]["lat"], e.STORE_BY_ID[sid]["lng"], 1.0,
                              MORNING, fresh, self.now)
        self.assertEqual(found["status"], "found")
        self.assertEqual(found["stores"][0]["source"], "community")
        # a report older than the snapshot must not override it
        age = e.STORE_BY_ID[sid]["inv"]["batt_d"]["age"]
        stale = {(sid, "batt_d"): {"status": "in_stock", "ts": self.now - (age + 600) * 60}}
        res = e.stock_of(e.STORE_BY_ID[sid], "batt_d", stale, self.now)
        self.assertEqual(res[2], "snapshot")

    def test_unexpired_report_stays_authoritative_while_time_passes(self):
        base = 1_700_000_000
        with unittest.mock.patch.object(e, "SNAPSHOT_TS", float(base)):
            sid = next(s["id"] for s in e.STORES if s["inv"].get("insulin", {}).get("q", 0) > 0)
            shop = e.STORE_BY_ID[sid]
            record = dict(shop["inv"]["insulin"])
            reports = report_store.LocalReportStore()
            reports.put(sid, "insulin", "out", ts=base)           # newer than the record, which is `age` minutes older
            for minutes in (0, 48, 5 * 60 + 59):                  # the inventory record never changes
                now = base + minutes * 60
                self.assertEqual(e.stock_of(shop, "insulin", reports.all_recent(now), now), (0, minutes, "community"), minutes)
            now = base + report_store.REPORT_TTL_S + 60            # expired: the unchanged snapshot is back, and it has aged
            self.assertEqual(e.stock_of(shop, "insulin", reports.all_recent(now), now),
                             (record["q"], record["age"] + report_store.REPORT_TTL_S // 60 + 1, "snapshot"))

    def test_snapshot_age_is_measured_from_one_stable_time(self):
        base = 1_700_000_000
        with unittest.mock.patch.object(e, "SNAPSHOT_TS", float(base)):
            shop = {"id": "x", "inv": {"a": {"q": 5, "age": 30}}}
            self.assertEqual(e.stock_of(shop, "a", {}, base)[1], 30)
            self.assertEqual(e.stock_of(shop, "a", {}, base + 600)[1], 40)     # ten minutes later it is ten minutes older
            self.assertEqual(e.stock_of(shop, "a", {}, base - 600)[1], 20)

    def test_a_newer_inventory_observation_beats_an_older_report(self):
        base = 1_700_000_000
        shop = {"id": "x", "inv": {"a": {"q": 9, "age": 0, "ts": base + 100}}}
        older = {("x", "a"): {"status": "out", "ts": base + 50}}
        newer = {("x", "a"): {"status": "out", "ts": base + 150}}
        self.assertEqual(e.stock_of(shop, "a", older, base + 200), (9, 1, "snapshot"))
        self.assertEqual(e.stock_of(shop, "a", newer, base + 200), (0, 0, "community"))

    def test_store_closed_report(self):
        top = self.search("insulin")["stores"][0]["id"]
        rep = {(top, "*"): {"status": "closed", "ts": self.now}}
        self.assertNotIn(top, [s["id"] for s in self.search("insulin", reports=rep)["stores"]])

    def test_partial_when_quantity_short(self):
        res = e.search_item("insulin", 999, DOWNTOWN[0], DOWNTOWN[1], 8, MORNING, {}, self.now)
        self.assertEqual(res["status"], "partial")

    def test_nearest_store_wins_over_a_slightly_farther_fresher_one(self):
        # Stock freshness is only a gentle tie-breaker: a store that is clearly closer and has the
        # item in stock should be chosen even if a farther store's shelf was checked more recently.
        def shop(sid, dlat, age):
            return {"id": sid, "name": sid, "type": "supermarket", "hood": "x",
                    "lat": DOWNTOWN[0] + dlat, "lng": DOWNTOWN[1], "hours": {"always": True},
                    "status": "open", "note": "", "inv": {"batt_aa": {"q": 9, "age": age}}}
        near_recent = shop("near_recent", 0.004, age=120)   # ~0.4 km, stock checked 2 h ago
        far_fresh = shop("far_fresh", 0.02, age=5)          # ~2.2 km, checked 5 min ago
        with unittest.mock.patch.object(e, "STORES", [near_recent, far_fresh]):
            res = e.search_item("batt_aa", 1, *DOWNTOWN, 8, MORNING, {}, self.now)
        self.assertEqual(res["stores"][0]["id"], "near_recent",
                         "the clearly-nearer in-stock store should rank first despite slightly older stock")

    def plan_for(self, item, qty, reports=None):
        return e.build_plan([need(item, 1, qty)], [], *DOWNTOWN, 8, MORNING, reports or {}, self.now)

    def test_store_that_can_fill_the_quantity_is_preferred_over_a_nearer_short_one(self):
        stores = self.search("insulin", qty=4)["stores"]
        self.assertTrue(any(s["short"] for s in stores) and any(not s["short"] for s in stores),
                        "demo data should offer both a short and a full store")
        plan = self.plan_for("insulin", 4)
        items = [i for s in plan["route"]["stops"] for i in s["items"]]
        self.assertEqual(len(items), 1)
        self.assertEqual((items[0]["qty"], items[0]["qty_needed"]), (4, 4))
        self.assertGreaterEqual(items[0]["q"], 4)
        self.assertFalse(items[0]["short"])
        self.assertEqual(plan["shortfalls"], [])
        self.assertEqual((plan["summary"]["covered"], plan["summary"]["short"]), (1, 0))

    def test_quantity_is_split_across_stops_when_no_single_store_can_fill_it(self):
        near = [s["id"] for s in self.search("insulin")["stores"][:2]]
        reports = {(s["id"], "insulin"): {"status": "in_stock" if s["id"] in near else "out", "ts": self.now - 30}
                   for s in e.STORES if "insulin" in s["inv"]}                    # exactly two stores, 5 units each
        plan = self.plan_for("insulin", 8, reports)
        stops = [(s["store"]["id"], i["qty"]) for s in plan["route"]["stops"] for i in s["items"]]
        self.assertEqual(sorted(sid for sid, _ in stops), sorted(near))
        self.assertEqual(sorted(q for _, q in stops), [3, 5])
        self.assertTrue(all(i["qty_needed"] == 8 for s in plan["route"]["stops"] for i in s["items"]))
        self.assertEqual(plan["shortfalls"], [])
        self.assertEqual(plan["summary"]["covered"], 1)

    def test_request_larger_than_all_available_stock_keeps_the_shortage(self):
        plan = self.plan_for("insulin", 99)
        units = sum(i["qty"] for s in plan["route"]["stops"] for i in s["items"])
        self.assertGreater(units, 0)
        self.assertLess(units, 99)
        (short,) = plan["shortfalls"]
        self.assertEqual((short["item_id"], short["found"], short["missing"]), ("insulin", units, 99 - units))
        self.assertEqual((plan["summary"]["covered"], plan["summary"]["short"], plan["summary"]["unresolved"]), (0, 1, 0))
        text = e.render_plan_text(plan)
        self.assertIn("Not enough in stock: Insulin (prescription), found %d of 99" % units, text)
        self.assertIn("x%d of 99" % plan["route"]["stops"][0]["items"][0]["qty"], text)

    def test_best_full_store_survives_the_candidate_limit(self):
        def shop(n, dlat, q):
            return {"id": n, "name": n, "type": "supermarket", "hood": "x", "lat": DOWNTOWN[0] + dlat, "lng": DOWNTOWN[1],
                    "hours": {"always": True}, "status": "open", "note": "", "inv": {"insulin": {"q": q, "age": 5}}}
        shops = [shop("near%d" % i, 0.001 * (i + 1), 1) for i in range(9)] + [shop("far_full", 0.04, 10)]
        with unittest.mock.patch.object(e, "STORES", shops):
            res = e.search_item("insulin", 4, DOWNTOWN[0], DOWNTOWN[1], 10, MORNING, {}, self.now, limit=8)
        ids = [s["id"] for s in res["stores"]]
        self.assertEqual(len(ids), 8)
        self.assertIn("far_full", ids)
        self.assertEqual(res["status"], "found")

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
        # Relief water points now keep bottled water downtown, so force it out everywhere to create
        # the gap this test needs and exercise the substitute swap deterministically.
        reports = {(s["id"], "water_bottled"): {"status": "out", "ts": self.now} for s in e.STORES}
        plan = e.build_plan([need("water_bottled", 1)], alts, *DOWNTOWN, 8, MORNING, reports, self.now)
        items = [i for s in plan["route"]["stops"] for i in s["items"]]
        self.assertEqual(len(items), 1)
        self.assertTrue(items[0]["is_alt"])
        self.assertEqual(items[0]["replaces"], "water_bottled")
        self.assertEqual(plan["summary"]["swapped"], 1)
        self.assertEqual(plan["alternatives"][0]["options"][0]["status"], "found")

    def test_multi_unit_substitutes_keep_the_original_quantity_unresolved(self):
        alts = [{"for_item": "baby_formula_powder", "options": [
            {"item_id": "baby_formula_rtf", "fit": "strong", "qty": 1, "quantity_verified": True}]}]
        for qty in (4, 99):
            with self.subTest(qty=qty):
                plan = e.build_plan([need("baby_formula_powder", 1, qty)], alts, *DOWNTOWN, 8, MORNING, {}, self.now)
                self.assertEqual(plan["route"]["stops"], [])
                self.assertEqual((plan["summary"]["covered"], plan["summary"]["unresolved"]), (0, 1))
                unresolved, = plan["unresolved"]
                self.assertEqual((unresolved["item_id"], unresolved["qty"], unresolved["reason"]),
                                 ("baby_formula_powder", qty, "quantity_unverified"))
                alt, = plan["alternatives"]
                self.assertEqual(alt["qty_requested"], qty)
                self.assertEqual([o["item_id"] for o in alt["options"]], ["baby_formula_rtf"])
                self.assertIn("Confirm package sizes", alt["quantity_note"])
                text = e.render_plan_text(plan)
                self.assertIn("Quantity not confirmed:", text)
                self.assertIn(f"You requested {qty} units", text)

    def test_unstocked_substitute_is_not_presented_as_a_quantity_check(self):
        reports = {(s["id"], "baby_formula_rtf"): {"status": "out", "ts": self.now} for s in e.STORES}
        alts = [{"for_item": "baby_formula_powder", "options": [{"item_id": "baby_formula_rtf"}]}]
        plan = e.build_plan([need("baby_formula_powder", 1, 4)], alts, *DOWNTOWN, 8, MORNING, reports, self.now)
        self.assertEqual(plan["unresolved"][0]["reason"], "no_stock")
        self.assertEqual(plan["alternatives"][0]["options"], [])

    def test_incompatible_fuels_are_rejected_even_from_client_alternatives(self):
        fuels = ("propane_1lb", "propane_20lb", "butane_canister", "canned_fuel", "charcoal")
        for missing in fuels:
            with self.subTest(missing=missing):
                reports = {(s["id"], missing): {"status": "out", "ts": self.now} for s in e.STORES}
                alts = [{"for_item": missing, "options": [{"item_id": other} for other in fuels if other != missing]}]
                plan = e.build_plan([need(missing)], alts, *DOWNTOWN, 8, MORNING, reports, self.now)
                self.assertEqual(plan["alternatives"][0]["options"], [])
                self.assertEqual(plan["route"]["stops"], [])
                self.assertEqual(plan["summary"]["unresolved"], 1)

    def test_generated_catalog_preserves_fuel_compatibility_rules(self):
        import runpy
        from pathlib import Path
        generated = runpy.run_path(str(Path(__file__).resolve().parent.parent / "scripts" / "gen_data.py"))["CAT"]
        for item in ("propane_1lb", "propane_20lb", "butane_canister", "canned_fuel", "charcoal"):
            self.assertEqual(generated[item]["safe_subs"], e.CATALOG[item]["safe_subs"], item)

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


class BundlingTests(unittest.TestCase):
    """With several items, prefer one store that stocks them all; otherwise bundle as many
    as possible per stop; if an item is nowhere, fall back to the usual gap behavior."""

    def setUp(self):
        self.now = time.time()

    @staticmethod
    def shop(sid, dlat, inv):
        return {"id": sid, "name": sid, "type": "supermarket", "hood": "x",
                "lat": DOWNTOWN[0] + dlat, "lng": DOWNTOWN[1],
                "hours": {"always": True}, "status": "open", "note": "",
                "inv": {k: {"q": q, "age": 5} for k, q in inv.items()}}

    def only(self, *shops):
        stack = contextlib.ExitStack()
        stack.enter_context(unittest.mock.patch.object(e, "STORES", list(shops)))
        stack.enter_context(unittest.mock.patch.object(e, "STORE_BY_ID", {s["id"]: s for s in shops}))
        return stack

    def plan(self, items):
        needs = [need(i) for i in items]
        return e.build_plan(needs, [], *DOWNTOWN, 8, MORNING, {}, self.now)

    def test_one_store_with_everything_is_a_single_stop(self):
        # A nearer store has only one item; a slightly farther store has all three.
        # Bundling should win: one stop at the all-in-one store, not three scattered stops.
        near_one = self.shop("near_one", 0.002, {"insulin": 9})
        all_three = self.shop("all_three", 0.02, {"insulin": 9, "diapers": 9, "food_canned": 9})
        with self.only(near_one, all_three):
            plan = self.plan(["insulin", "diapers", "food_canned"])
        self.assertEqual(len(plan["route"]["stops"]), 1)
        stop = plan["route"]["stops"][0]
        self.assertEqual(stop["store"]["id"], "all_three")
        self.assertEqual(sorted(i["item_id"] for i in stop["items"]),
                         ["diapers", "food_canned", "insulin"])
        self.assertEqual(plan["unresolved"], [])

    def test_bundle_as_much_as_possible_when_no_single_store_has_all(self):
        # No store has all three; two stores each carry two of them (one overlapping).
        # The plan should cover everything in two stops, not three.
        a = self.shop("a", 0.004, {"insulin": 9, "diapers": 9})
        b = self.shop("b", 0.02, {"diapers": 9, "food_canned": 9})
        with self.only(a, b):
            plan = self.plan(["insulin", "diapers", "food_canned"])
        placed = {i["item_id"] for s in plan["route"]["stops"] for i in s["items"]}
        self.assertEqual(placed, {"insulin", "diapers", "food_canned"})
        self.assertEqual(len(plan["route"]["stops"]), 2)
        self.assertEqual(plan["unresolved"], [])

    def test_item_available_nowhere_falls_back_to_the_usual_gap(self):
        # One store bundles two items; the third is stocked nowhere, so it stays unresolved
        # exactly as before, while the two that exist are still bundled into one stop.
        only_two = self.shop("only_two", 0.01, {"insulin": 9, "diapers": 9})
        with self.only(only_two):
            plan = self.plan(["insulin", "diapers", "food_canned"])
        self.assertEqual(len(plan["route"]["stops"]), 1)
        stop = plan["route"]["stops"][0]
        self.assertEqual(sorted(i["item_id"] for i in stop["items"]), ["diapers", "insulin"])
        self.assertEqual([(u["item_id"], u["reason"]) for u in plan["unresolved"]],
                         [("food_canned", "no_stock")])


class TravelModeTests(unittest.TestCase):
    """Driving and walking are two plans over the same stores: different range, different times,
    and a store that is open when a car arrives can be closed by the time a walker does."""

    def setUp(self):
        self.now = time.time()

    @staticmethod
    def shop(sid, dlat, close_min=22 * 60):
        return {"id": sid, "name": sid, "type": "supermarket", "hood": "x", "lat": DOWNTOWN[0] + dlat, "lng": DOWNTOWN[1],
                "hours": {"open": 6 * 60, "close": close_min}, "status": "open", "note": "",
                "inv": {"insulin": {"q": 10, "age": 5}}}

    def only(self, *shops):
        """Replace the demo stores with these."""
        stack = contextlib.ExitStack()
        stack.enter_context(unittest.mock.patch.object(e, "STORES", list(shops)))
        stack.enter_context(unittest.mock.patch.object(e, "STORE_BY_ID", {s["id"]: s for s in shops}))
        return stack

    def plan(self, mode, item="insulin"):
        return e.build_plan([need(item, 1)], [], *DOWNTOWN, 8, MORNING, {}, self.now, mode=mode)

    def test_walking_is_much_slower_than_driving_and_limited_in_range(self):
        self.assertEqual(eta_min(2.4), 7)
        self.assertEqual(eta_min(2.4, "walking"), 45)
        self.assertEqual((reach_km(8), reach_km(8, "walking"), reach_km(1.5, "walking")), (8, WALK_MAX_KM, 1.5))
        with self.assertRaises(KeyError):
            eta_min(1, "hovercraft")

    def test_walking_search_uses_walking_range_and_times(self):
        walk = e.search_item("insulin", 1, *DOWNTOWN, 8, MORNING, {}, self.now, mode="walking")
        drive = e.search_item("insulin", 1, *DOWNTOWN, 8, MORNING, {}, self.now)
        self.assertTrue(walk["stores"])
        for s in walk["stores"]:
            d = haversine_km(*DOWNTOWN, s["lat"], s["lng"])
            self.assertLessEqual(d, WALK_MAX_KM)
            self.assertEqual(s["eta_min"], eta_min(d, "walking"))
        self.assertTrue(any(haversine_km(*DOWNTOWN, s["lat"], s["lng"]) > WALK_MAX_KM for s in drive["stores"]),
                        "the driving search should reach stores a walker would not be sent to")

    def test_walking_plan_is_its_own_plan(self):
        walk, drive = self.plan("walking"), self.plan("driving")
        self.assertEqual((walk["mode"], walk["route"]["mode"], drive["mode"], drive["route"]["mode"]),
                         ("walking", "walking", "driving", "driving"))
        self.assertEqual((walk["location"]["radius_km"], drive["location"]["radius_km"]), (WALK_MAX_KM, 8))
        self.assertGreater(walk["route"]["total_travel_min"], drive["route"]["total_travel_min"])
        self.assertEqual(walk["route"]["total_travel_min"], walk["route"]["total_drive_min"])   # the original field name
        self.assertIn("on foot", e.render_plan_text(walk))
        self.assertIn(" min walk,", e.render_plan_text(walk))
        self.assertIn(" min drive,", e.render_plan_text(drive))
        self.assertNotIn("on foot", e.render_plan_text(drive))

    def test_a_store_open_for_a_car_can_be_closed_for_a_walker(self):
        # 2.5 km away, closes at 10:40. A car arrives at 10:07; a walker would arrive at 10:47.
        with self.only(self.shop("closing", 0.0225, close_min=10 * 60 + 40)):
            drive, walk = self.plan("driving"), self.plan("walking")
        self.assertEqual([s["store"]["id"] for s in drive["route"]["stops"]], ["closing"])
        self.assertEqual(walk["route"]["stops"], [])
        self.assertEqual([(u["item_id"], u["reason"]) for u in walk["unresolved"]], [("insulin", "no_stock")])

    def test_nobody_is_sent_beyond_walking_range(self):
        with self.only(self.shop("far", 0.045)):                      # about 5 km
            drive, walk = self.plan("driving"), self.plan("walking")
        self.assertEqual([s["store"]["id"] for s in drive["route"]["stops"]], ["far"])
        self.assertEqual(walk["route"]["stops"], [])

    def test_alternatives_are_checked_against_the_walking_range_too(self):
        alts = [{"for_item": "water_bottled", "options": [{"item_id": "water_filter"}]}]
        near, far = self.shop("near", 0.009), self.shop("far", 0.045)
        near["inv"] = {"water_filter": {"q": 9, "age": 5}}
        far["inv"] = {"water_filter": {"q": 9, "age": 5}}
        for shops, expect_swap in (((near,), True), ((far,), False)):
            with self.only(*shops):
                plan = e.build_plan([need("water_bottled", 1)], alts, *DOWNTOWN, 8, MORNING, {}, self.now, mode="walking")
            self.assertEqual(bool(plan["alternatives"][0]["options"]), expect_swap, [s.get("id") for s in shops])


if __name__ == "__main__":
    unittest.main()
