"""Tests for the Bedrock layer using a scripted fake bedrock-runtime client.

These verify OUR code around Bedrock (request shape, JSON validation, repair
retry, model fallback chain, safety enforcement, rules fallback). They do not
call AWS; see scripts/bedrock_check.py for a real end-to-end call.
"""
import contextlib
import json
import os
import sys
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
os.environ["AI_MODE"] = "auto"
os.environ.pop("BEDROCK_MODEL_IDS", None)

import ai  # noqa: E402
import engine  # noqa: E402

try:
    from botocore.exceptions import ClientError, NoCredentialsError
except ImportError:  # pragma: no cover
    ClientError = NoCredentialsError = None


class FakeBedrock:
    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def converse(self, **kw):
        self.calls.append(kw)
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        if not isinstance(item, str):
            item = json.dumps(item)
        return {"output": {"message": {"role": "assistant", "content": [{"text": item}]}},
                "usage": {"inputTokens": 120, "outputTokens": 60}, "metrics": {"latencyMs": 5}}


class Base(unittest.TestCase):
    """Each test starts with Bedrock enabled and a clean health memory (other test
    modules set AI_MODE=off at import time, so never rely on module state)."""
    def setUp(self):
        os.environ["AI_MODE"] = "auto"
        os.environ.pop("BEDROCK_MODEL_IDS", None)
        ai.set_client(None)


def client_error(code):
    return ClientError({"Error": {"Code": code, "Message": "nope"}}, "Converse")


INTENT = {
    "disaster": {"type": "wildfire", "summary": "Wildfire warning with a power outage."},
    "household": {"people": 3, "infants": True, "elderly": True, "pets": False, "medical": ["insulin"]},
    "urgent_safety": {"flag": False, "reason": ""},
    "needs": [
        {"item_id": "insulin", "label": "insulin", "qty": 1, "priority": 1, "inferred": False, "why": ""},
        {"item_id": "cooler", "label": "cooler", "qty": 1, "priority": 1, "inferred": True, "why": "Insulin must stay cool."},
        {"item_id": "not_a_real_item", "label": "flux capacitor", "qty": 1, "priority": 1},
    ],
    "unmapped": ["hover board"],
}


class UnderstandTests(Base):
    def test_happy_path_validates_and_traces(self):
        fake = FakeBedrock([INTENT])
        ai.set_client(fake)
        intent, trace = ai.understand("My dad needs insulin and the power is out")
        self.assertEqual([n["item_id"] for n in intent["needs"]], ["insulin", "cooler"])
        self.assertIn("flux capacitor", intent["unmapped"])
        self.assertEqual(trace["mode"], "bedrock")
        self.assertEqual(trace["model"], ai.model_chain()[0])
        self.assertEqual(trace["tokens_in"], 120)
        # request shape: Converse with system prompt containing the catalog, and a user message
        kw = fake.calls[0]
        self.assertEqual(kw["modelId"], ai.model_chain()[0])
        self.assertIn("insulin | Insulin (prescription)", kw["system"][0]["text"])
        self.assertIn("insulin", kw["messages"][0]["content"][0]["text"])
        self.assertIn("maxTokens", kw["inferenceConfig"])

    def test_json_in_code_fence_and_prose_is_accepted(self):
        ai.set_client(FakeBedrock(["Sure! Here you go:\n```json\n" + json.dumps(INTENT) + "\n```\nHope that helps."]))
        intent, trace = ai.understand("insulin please")
        self.assertEqual(trace["mode"], "bedrock")
        self.assertTrue(intent["needs"])

    def test_invalid_json_triggers_one_repair_retry_on_same_model(self):
        fake = FakeBedrock(["I think you need some water!", INTENT])
        ai.set_client(fake)
        _, trace = ai.understand("insulin please")
        self.assertEqual(len(fake.calls), 2)
        self.assertEqual(trace["model"], ai.model_chain()[0])
        self.assertEqual(trace["calls"], 2)
        # the repair turn carries the bad reply plus a correction request
        roles = [m["role"] for m in fake.calls[1]["messages"]]
        self.assertEqual(roles, ["user", "assistant", "user"])

    def test_unknown_item_ids_only_triggers_repair_then_fallback_chain(self):
        bad = {"needs": [{"item_id": "nope", "label": "x"}]}
        fake = FakeBedrock([bad] * 8)
        ai.set_client(fake)
        intent, trace = ai.understand("I need water and batteries")
        self.assertEqual(trace["mode"], "rules")
        self.assertIn("water_bottled", [n["item_id"] for n in intent["needs"]])

    @unittest.skipIf(ClientError is None, "botocore not installed")
    def test_model_access_denied_falls_through_to_next_model(self):
        fake = FakeBedrock([client_error("AccessDeniedException"), INTENT])
        ai.set_client(fake)
        _, trace = ai.understand("insulin please")
        self.assertEqual(trace["model"], ai.model_chain()[1])
        self.assertEqual(trace["fell_back_from"], [ai.model_chain()[0]])
        # the failed model is remembered and skipped next time
        fake.script = [INTENT]
        _, trace2 = ai.understand("insulin please")
        self.assertEqual(trace2["model"], ai.model_chain()[1])
        self.assertEqual(fake.calls[-1]["modelId"], ai.model_chain()[1])

    @unittest.skipIf(ClientError is None, "botocore not installed")
    def test_throttling_tries_next_model(self):
        fake = FakeBedrock([client_error("ThrottlingException"), INTENT])
        ai.set_client(fake)
        _, trace = ai.understand("insulin please")
        self.assertEqual(trace["model"], ai.model_chain()[1])

    @unittest.skipIf(NoCredentialsError is None, "botocore not installed")
    def test_no_credentials_uses_rules_and_stops_calling(self):
        fake = FakeBedrock([NoCredentialsError()])
        ai.set_client(fake)
        intent, trace = ai.understand("I need insulin and a can opener")
        self.assertEqual(trace["mode"], "rules")
        self.assertEqual(len(fake.calls), 1)
        ai.understand("again")
        self.assertEqual(len(fake.calls), 1)  # cached: no further calls for a minute

    def test_ai_mode_off_never_calls_the_client(self):
        fake = FakeBedrock([])
        ai.set_client(fake)
        os.environ["AI_MODE"] = "off"
        try:
            _, trace = ai.understand("water and batteries")
        finally:
            os.environ["AI_MODE"] = "auto"
        self.assertEqual(trace["mode"], "rules")
        self.assertEqual(fake.calls, [])

    def test_keyword_safety_net_works_without_the_model(self):
        ai.set_client(FakeBedrock([INTENT]))  # model says no danger
        intent, _ = ai.understand("I smell gas after the earthquake, need water")
        self.assertTrue(intent["urgent_safety"]["flag"])
        self.assertIn("911", intent["urgent_safety"]["reason"])

    def test_rules_fallback_infers_cooler_for_insulin(self):
        ai.set_client(FakeBedrock([]))
        os.environ["AI_MODE"] = "off"
        try:
            intent, _ = ai.understand("Wildfire. Power is out and my dad needs insulin. We also have canned food.")
        finally:
            os.environ["AI_MODE"] = "auto"
        ids = {n["item_id"] for n in intent["needs"]}
        self.assertTrue({"insulin", "cooler", "ice_bag", "food_canned", "can_opener", "n95_mask"} <= ids)
        self.assertEqual(intent["disaster"]["type"], "wildfire")


class DeadlineTests(Base):
    """Bedrock attempts must fit in the time the request has left (Lambda: 29 s, API Gateway: 30 s)."""

    @staticmethod
    def slow_client(clock, seconds, starts=None):
        class SlowBedrock:
            def converse(self, **kw):
                if starts is not None:
                    starts.append(clock[0])
                clock[0] += seconds
                return {"output": {"message": {"content": [{"text": "not JSON"}]}}}
        return SlowBedrock()

    def test_a_repair_that_cannot_fit_is_not_started(self):
        clock, starts = [0.0], []
        ai.set_client(self.slow_client(clock, 20, starts))
        with mock.patch.object(ai.time, "monotonic", side_effect=lambda: clock[0]):
            _, trace = ai.understand("I need water")
        self.assertEqual(starts, [0.0])            # no repair at 20 s and no second model
        self.assertEqual((trace["mode"], trace["reason"]), ("rules", "time budget used up"))
        self.assertEqual(clock[0], 20)             # the fallback answered right after the first call

    def test_each_attempt_is_asked_to_finish_inside_the_time_left(self):
        clock, asked = [0.0], []
        bedrock = self.slow_client(clock, 9)

        def client_for(timeout_s):
            asked.append(timeout_s)
            return bedrock
        with mock.patch.object(ai, "_get_client", side_effect=client_for), \
                mock.patch.object(ai.time, "monotonic", side_effect=lambda: clock[0]):
            ai.understand("I need water")
        self.assertEqual(asked, [24.0, 15.0, 6.0])  # 24 s stage budget, minus 9 s per call; a 4th call would have -3 s

    def test_request_deadline_reserves_time_for_the_fallback(self):
        fake = FakeBedrock([INTENT])
        ai.set_client(fake)
        ai.set_deadline(7.0)                        # 7 s left in the invocation, minus the 3 s reserve, is under the 5 s minimum
        _, trace = ai.understand("I need water")
        self.assertEqual((fake.calls, trace["mode"]), ([], "rules"))
        ai.set_deadline(40.0)                       # plenty: the 24 s stage budget is the only limit
        _, trace = ai.understand("I need water")
        self.assertEqual((len(fake.calls), trace["mode"]), (1, "bedrock"))

    def test_handler_passes_the_lambda_remaining_time_to_the_ai_layer(self):
        import handler

        class Context:
            def get_remaining_time_in_millis(self):
                return 6000
        seen = []
        with mock.patch.object(ai, "set_deadline", side_effect=seen.append):
            handler.handler({"rawPath": "/api/health", "requestContext": {"http": {"method": "GET"}}}, Context())
        self.assertEqual(seen, [6.0, None])         # set for the request, cleared afterwards
        fake = FakeBedrock([INTENT])
        ai.set_client(fake)
        event = {"rawPath": "/api/understand", "requestContext": {"http": {"method": "POST", "sourceIp": "deadline-test"}},
                 "body": json.dumps({"text": "I need water and batteries"})}
        body = json.loads(handler.handler(event, Context())["body"])
        self.assertEqual((fake.calls, body["trace"]["mode"]), ([], "rules"))   # too little time: no Bedrock call at all
        self.assertTrue(body["intent"]["needs"])

    @unittest.skipIf(ClientError is None, "botocore not installed")
    def test_real_client_never_retries_and_times_out_inside_the_time_left(self):
        ai.set_client(None)
        for asked in (24.0, 8.4, 5.0):
            config = ai._get_client(asked).meta.config
            self.assertEqual(config.retries["total_max_attempts"], 1, asked)   # max_attempts=1 would still allow one retry
            self.assertLessEqual(config.connect_timeout + config.read_timeout, min(asked, ai.MAX_ATTEMPT_S), asked)


class AlternativesTests(Base):
    POOL = {k: {"dist_km": 3.0 + i, "name": "x"} for i, k in enumerate(
        ["water_filter", "water_tabs", "water_jug", "lantern_solar_usb", "headlamp", "insulin", "baby_formula_rtf"])}

    def test_validates_ids_enforces_policy_and_fills_cautions(self):
        reply = {"alternatives": [
            {"for_item": "water_bottled", "verdict": "substitutes_available",
             "options": [
                 {"item_id": "water_filter", "fit": "strong", "why": "Removes germs.", "how_to_use": "Filter it.", "caution": ""},
                 {"item_id": "made_up_item", "fit": "strong", "why": "", "how_to_use": "", "caution": ""},
                 {"item_id": "headlamp", "fit": "wow", "why": "not water", "how_to_use": "", "caution": ""},  # not in pool for this test? it is, kept
             ], "no_purchase_tips": ["Boil water if you can."], "escalate": ""},
            {"for_item": "insulin", "verdict": "substitutes_available",
             "options": [{"item_id": "water_jug", "fit": "strong", "why": "", "how_to_use": "", "caution": ""}],
             "no_purchase_tips": [], "escalate": ""},
        ]}
        ai.set_client(FakeBedrock([reply]))
        gaps = [{"item_id": "water_bottled", "label": "water", "priority": 1, "why": ""},
                {"item_id": "insulin", "label": "insulin", "priority": 1, "why": ""}]
        alts, trace = ai.alternatives(gaps, self.POOL, {"disaster": {"type": "earthquake"}, "household": {}})
        self.assertEqual(trace["mode"], "bedrock")
        by = {a["for_item"]: a for a in alts}
        self.assertEqual([o["item_id"] for o in by["water_bottled"]["options"]], ["water_filter", "headlamp"])
        self.assertEqual(by["water_bottled"]["options"][1]["fit"], "partial")  # invalid fit coerced
        self.assertEqual(by["insulin"]["options"], [])                          # policy none: enforced in code
        self.assertEqual(by["insulin"]["verdict"], "no_safe_substitute")
        self.assertIn("pharmacy", by["insulin"]["escalate"])
        self.assertEqual(by["water_bottled"]["no_purchase_tips"], ["Boil water if you can."])

    def test_caution_items_only_accept_curated_substitutes(self):
        pool = {"baby_formula_rtf": {"dist_km": 1.0}, "baby_food": {"dist_km": 0.5}, "diapers": {"dist_km": 0.4}}
        reply = {"alternatives": [{"for_item": "baby_formula_powder", "verdict": "substitutes_available",
                                   "options": [{"item_id": "baby_food", "fit": "strong", "why": "x", "how_to_use": "", "caution": ""},
                                               {"item_id": "baby_formula_rtf", "fit": "strong", "why": "y", "how_to_use": "", "caution": ""}],
                                   "no_purchase_tips": [], "escalate": ""}]}
        fake = FakeBedrock([reply])
        ai.set_client(fake)
        alts, _ = ai.alternatives([{"item_id": "baby_formula_powder", "label": "formula", "priority": 1, "why": ""}], pool, {})
        self.assertEqual([o["item_id"] for o in alts[0]["options"]], ["baby_formula_rtf"])
        self.assertTrue(alts[0]["options"][0]["caution"])  # catalog caution filled in when the model omits it
        prompt = fake.calls[0]["messages"][0]["content"][0]["text"]
        self.assertIn('"allowed_substitutes": ["baby_formula_rtf"]', prompt)

    def test_model_cannot_offer_incompatible_fuel_for_a_camp_canister(self):
        pool = {"propane_20lb": {"dist_km": 0.5}, "butane_canister": {"dist_km": 2.0},
                "canned_fuel": {"dist_km": 1.0}, "food_meals": {"dist_km": 3.0}}
        reply = {"alternatives": [{"for_item": "propane_1lb", "options": [
            {"item_id": "propane_20lb", "fit": "strong", "why": "", "how_to_use": "", "caution": ""},
            {"item_id": "butane_canister", "fit": "partial", "why": "", "how_to_use": "", "caution": ""},
            {"item_id": "canned_fuel", "fit": "partial"},
            {"item_id": "food_meals", "fit": "workaround"}]}]}
        ai.set_client(FakeBedrock([reply]))
        alts, _ = ai.alternatives([{"item_id": "propane_1lb", "label": "propane", "priority": 2, "why": ""}], pool, {})
        self.assertEqual([o["item_id"] for o in alts[0]["options"]], ["food_meals"])

    def test_fallback_never_suggests_baby_food_for_formula(self):
        os.environ["AI_MODE"] = "off"
        try:
            ai.set_client(FakeBedrock([]))
            pool = {"baby_food": {"dist_km": 0.2}, "baby_formula_rtf": {"dist_km": 3.0}}
            alts, _ = ai.alternatives([{"item_id": "baby_formula_powder", "label": "formula", "priority": 1, "why": ""}], pool, {})
        finally:
            os.environ["AI_MODE"] = "auto"
        self.assertEqual([o["item_id"] for o in alts[0]["options"]], ["baby_formula_rtf"])

    def test_caution_item_with_empty_allowlist_gets_no_substitutes_but_guidance(self):
        pool = {"headlamp": {"dist_km": 1.0}}
        reply = {"alternatives": [{"for_item": "pain_reliever", "options": [
            {"item_id": "headlamp", "fit": "strong", "why": "", "how_to_use": "", "caution": ""}]}]}
        ai.set_client(FakeBedrock([reply]))
        alts, _ = ai.alternatives([{"item_id": "pain_reliever", "label": "painkillers", "priority": 2, "why": ""}], pool, {})
        self.assertEqual(alts[0]["options"], [])
        self.assertIn("pharmacist", alts[0]["escalate"])

    def rules_only(self, item_id, pool):
        os.environ["AI_MODE"] = "off"
        try:
            ai.set_client(FakeBedrock([]))
            alts, trace = ai.alternatives([{"item_id": item_id, "label": item_id, "priority": 2, "why": ""}], pool, {})
        finally:
            os.environ["AI_MODE"] = "auto"
        self.assertEqual(trace["mode"], "rules")
        return alts[0]

    def test_fallback_does_not_swap_other_battery_sizes_or_unrelated_gear(self):
        pool = {k: {"dist_km": 1.0 + i} for i, k in enumerate(
            ["batt_aa", "batt_aaa", "batt_d", "radio_crank", "lantern_solar_usb", "power_bank", "headlamp", "flashlight_led"])}
        for missing in ("batt_d", "batt_aa", "batt_aaa"):
            alt = self.rules_only(missing, {k: v for k, v in pool.items() if k != missing})
            self.assertEqual((alt["options"], alt["verdict"]), ([], "none_found"), missing)

    def test_fallback_offers_only_substitutes_that_serve_the_same_purpose(self):
        pool = {k: {"dist_km": 1.0 + i} for i, k in enumerate(
            ["headlamp", "radio_crank", "food_bars", "water_tabs", "water_filter", "power_bank", "water_jug"])}
        self.assertEqual([o["item_id"] for o in self.rules_only("water_bottled", pool)["options"]], ["water_filter", "water_tabs"])
        self.assertEqual([o["item_id"] for o in self.rules_only("radio_battery", pool)["options"]], ["radio_crank"])
        no_radio = {k: v for k, v in pool.items() if k != "radio_crank"}
        self.assertEqual(self.rules_only("radio_battery", no_radio)["options"], [])   # nothing else in stock is a radio
        lantern = self.rules_only("lantern_led_battery", {"lantern_solar_usb": {"dist_km": 3.0}, "radio_crank": {"dist_km": 0.5},
                                                        "batt_d": {"dist_km": 0.5}})
        self.assertEqual([(o["item_id"], o["fit"]) for o in lantern["options"]], [("lantern_solar_usb", "strong")])

    def test_fallback_table_is_consistent_with_the_catalog_and_its_safety_rules(self):
        for missing, subs in ai.FALLBACK_SUBS.items():
            self.assertIn(missing, ai.CATALOG, missing)
            self.assertNotEqual(ai.CATALOG[missing]["policy"], "none", f"{missing} must never be substituted")
            for s in subs:
                self.assertIn(s["item_id"], ai.CATALOG, f"{missing} -> {s['item_id']}")
                self.assertNotEqual(s["item_id"], missing)
                self.assertNotEqual(ai.CATALOG[s["item_id"]]["policy"], "none", f"{missing} -> {s['item_id']}")
                self.assertIn(s["fit"], ("strong", "partial", "workaround"))
                self.assertTrue(s["why"], f"{missing} -> {s['item_id']} needs an explanation")
                if ai.CATALOG[missing]["policy"] == "caution":
                    self.assertIn(s["item_id"], ai.CATALOG[missing]["safe_subs"], f"{missing} -> {s['item_id']} is not on its allowlist")

    def test_prompt_never_offers_prescription_items_as_candidates(self):
        fake = FakeBedrock([{"alternatives": [{"for_item": "water_bottled", "options": []}]}])
        ai.set_client(fake)
        ai.alternatives([{"item_id": "water_bottled", "label": "water", "priority": 1, "why": ""}], self.POOL, {})
        prompt = fake.calls[0]["messages"][0]["content"][0]["text"]
        avail = prompt.split("AVAILABLE")[1]
        self.assertNotIn('"item_id": "insulin"', avail)
        self.assertIn('"item_id": "water_filter"', avail)

    def test_fallback_offers_a_cooking_workaround_without_incompatible_fuels(self):
        pool = {k: {"dist_km": 2.0 + i} for i, k in enumerate(
            ["butane_canister", "camp_stove_propane", "canned_fuel", "headlamp", "food_meals"])}
        alt = self.rules_only("propane_1lb", pool)
        self.assertEqual([(o["item_id"], o["fit"]) for o in alt["options"]], [("food_meals", "workaround")])
        self.assertIn("does not provide fuel", alt["options"][0]["caution"])
        del pool["food_meals"]
        alt = self.rules_only("propane_1lb", pool)
        self.assertEqual((alt["options"], alt["verdict"]), ([], "none_found"))

    def test_fallback_does_not_swap_fuels_in_the_reverse_direction(self):
        pool = {k: {"dist_km": 1.0} for k in ("propane_1lb", "propane_20lb", "butane_canister", "canned_fuel", "charcoal")}
        for missing in ("butane_canister", "canned_fuel", "charcoal"):
            with self.subTest(missing=missing):
                self.assertEqual(self.rules_only(missing, pool)["options"], [])


class BriefTests(Base):
    def make_plan(self):
        needs = [{"item_id": "insulin", "label": "insulin", "qty": 1, "priority": 1, "inferred": False, "why": ""},
                 {"item_id": "diapers", "label": "diapers", "qty": 1, "priority": 2, "inferred": False, "why": ""}]
        return engine.build_plan(needs, [], 37.3382, -121.8863, 8, 600, {}, time.time())

    def test_brief_validated_and_911_enforced(self):
        plan = self.make_plan()
        n = len(plan["route"]["stops"])
        reply = {"headline": "Start with the pharmacy.", "summary": "Two stops.",
                 "steps": [{"stop": i + 1, "say": f"Do stop {i + 1}"} for i in range(n)] + [{"stop": 99, "say": "ghost"}],
                 "safety_notes": ["Keep insulin cool but not frozen."], "if_stuck": ["Dial 211."]}
        ai.set_client(FakeBedrock([reply]))
        intent = {"disaster": {"type": "wildfire"}, "household": {}, "urgent_safety": {"flag": True, "reason": "x"}}
        brief, trace = ai.brief(plan, intent)
        self.assertEqual(trace["mode"], "bedrock")
        self.assertEqual(len(brief["steps"]), n)
        self.assertIn("911", brief["safety_notes"][0])

    def test_brief_prompt_contains_only_plan_facts(self):
        plan = self.make_plan()
        fake = FakeBedrock([{"headline": "Go.", "steps": []}])
        ai.set_client(fake)
        ai.brief(plan, {"disaster": {}, "household": {}, "urgent_safety": {"flag": False}})
        prompt = fake.calls[0]["messages"][0]["content"][0]["text"]
        self.assertIn(plan["route"]["stops"][0]["store"]["name"], prompt)

    def test_brief_fallback_when_model_returns_garbage(self):
        plan = self.make_plan()
        ai.set_client(FakeBedrock(["nope"] * 8))
        brief, trace = ai.brief(plan, {"disaster": {"type": "wildfire"}, "household": {}, "urgent_safety": {"flag": False}})
        self.assertEqual(trace["mode"], "rules")
        self.assertTrue(brief["headline"])
        self.assertEqual(len(brief["steps"]), len(plan["route"]["stops"]))


class FollowupTests(Base):
    def test_followup_returns_plain_text(self):
        ai.set_client(FakeBedrock(["Yes. Keep the insulin cool, not frozen."]))
        ans, trace = ai.followup("Can the insulin go in the cooler?", "PLAN text")
        self.assertIn("insulin", ans)
        self.assertEqual(trace["mode"], "bedrock")


class AgentTests(Base):
    """Every task is its own agent: its own settings and, if wanted, its own model chain."""

    def setUp(self):
        super().setUp()
        patcher = mock.patch.dict(os.environ)
        patcher.start()
        self.addCleanup(patcher.stop)
        for var in [v for v in os.environ if v.startswith("BEDROCK_MODEL_IDS")]:
            os.environ.pop(var)

    def test_a_task_chain_beats_the_shared_chain_which_beats_the_defaults(self):
        self.assertEqual(ai.model_chain("brief"), ai.DEFAULT_MODELS)
        os.environ["BEDROCK_MODEL_IDS"] = "model-a, model-b"
        os.environ["BEDROCK_MODEL_IDS_BRIEF"] = "model-c"
        self.assertEqual(ai.model_chain("understand"), ["model-a", "model-b"])
        self.assertEqual(ai.model_chain("brief"), ["model-c"])
        self.assertEqual(ai.model_chain(), ["model-a", "model-b"])
        os.environ["BEDROCK_MODEL_IDS_BRIEF"] = " , "          # blank means "not set"
        self.assertEqual(ai.model_chain("brief"), ["model-a", "model-b"])

    def test_each_agent_calls_its_own_model_with_its_own_settings(self):
        os.environ["BEDROCK_MODEL_IDS_FOLLOWUP"] = "amazon.nova-lite-v1:0"
        fake = FakeBedrock([INTENT, "Yes, go now."])
        ai.set_client(fake)
        ai.understand("insulin please")
        ai.followup("Is it open?", "PLAN")
        understand_call, followup_call = fake.calls
        self.assertEqual(understand_call["modelId"], ai.DEFAULT_MODELS[0])
        self.assertEqual(followup_call["modelId"], "amazon.nova-lite-v1:0")
        for call, stage in ((understand_call, "understand"), (followup_call, "followup")):
            self.assertEqual(call["inferenceConfig"]["maxTokens"], ai.AGENTS[stage]["max_tokens"], stage)
            self.assertEqual(call["inferenceConfig"]["temperature"], ai.AGENTS[stage]["temperature"], stage)

    def test_a_model_that_is_not_enabled_is_skipped_by_every_agent_that_would_use_it_and_only_those(self):
        os.environ["BEDROCK_MODEL_IDS_FOLLOWUP"] = "amazon.nova-lite-v1:0"
        fake = FakeBedrock([client_error("AccessDeniedException"), INTENT, "ok", {"headline": "Go.", "steps": []}])
        ai.set_client(fake)
        ai.understand("insulin please")                       # the default first model is not enabled for this account
        ai.followup("Is it open?", "PLAN")                    # its own model, untouched by that
        needs = [{"item_id": "insulin", "label": "insulin", "qty": 1, "priority": 1, "inferred": False, "why": ""}]
        plan = engine.build_plan(needs, [], 37.3382, -121.8863, 8, 600, {}, time.time())
        ai.brief(plan, {"disaster": {}, "household": {}, "urgent_safety": {"flag": False}})   # remembers, skips the first
        self.assertEqual([c["modelId"] for c in fake.calls],
                         [ai.DEFAULT_MODELS[0], ai.DEFAULT_MODELS[1], "amazon.nova-lite-v1:0", ai.DEFAULT_MODELS[1]])

    def test_status_lists_every_agent_and_its_models(self):
        os.environ["BEDROCK_MODEL_IDS_BRIEF"] = "model-c"
        agents = ai.status()["agents"]
        self.assertEqual(set(agents), {"understand", "alternatives", "brief", "followup"})
        self.assertEqual(agents["brief"]["models"], ["model-c"])
        self.assertEqual(agents["followup"]["models"], ai.DEFAULT_MODELS)
        self.assertEqual(ai.status()["models"], ai.DEFAULT_MODELS)


class LaneTests(Base):
    """Driving and walking are separate lanes: each has its own substitutes and briefing agents."""

    def plan(self, mode):
        needs = [{"item_id": "insulin", "label": "insulin", "qty": 1, "priority": 1, "inferred": False, "why": ""}]
        return engine.build_plan(needs, [], 37.3382, -121.8863, 8, 600, {}, time.time(), mode=mode)

    INTENT_ARGS = {"disaster": {"type": "wildfire"}, "household": {}, "urgent_safety": {"flag": False}}

    def test_a_briefing_knows_its_lane_and_the_prompt_says_how_the_person_travels(self):
        fake = FakeBedrock([{"headline": "Go.", "steps": []}, {"headline": "Go.", "steps": []}])
        ai.set_client(fake)
        _, walk = ai.brief(self.plan("walking"), self.INTENT_ARGS)
        _, drive = ai.brief(self.plan("driving"), self.INTENT_ARGS)
        self.assertEqual((walk["agent"], walk["lane"], drive["lane"]), ("brief", "walking", "driving"))
        walk_prompt, drive_prompt = (c["messages"][0]["content"][0]["text"] for c in fake.calls)
        self.assertIn('"travel_mode": "walking"', walk_prompt)
        self.assertIn('"travel_mode": "driving"', drive_prompt)
        self.assertIn("never suggest driving", fake.calls[0]["system"][0]["text"])

    @staticmethod
    def only_a_far_pharmacy():
        """One store 2.5 km away: 7 minutes by car, 47 on foot."""
        shop = {"id": "far", "name": "Far Pharmacy", "type": "pharmacy", "hood": "x", "lat": 37.3382 + 0.0225,
                "lng": -121.8863, "hours": {"always": True}, "status": "open", "note": "",
                "inv": {"insulin": {"q": 10, "age": 5}}}
        stack = contextlib.ExitStack()
        stack.enter_context(mock.patch.object(engine, "STORES", [shop]))
        stack.enter_context(mock.patch.object(engine, "STORE_BY_ID", {"far": shop}))
        return stack

    def test_the_rules_briefing_for_a_walker_says_on_foot_and_adds_walking_advice(self):
        patcher = mock.patch.dict(os.environ, {"AI_MODE": "off"})
        patcher.start()
        self.addCleanup(patcher.stop)
        with self.only_a_far_pharmacy():
            walk, drive = self.plan("walking"), self.plan("driving")
        self.assertGreaterEqual(walk["route"]["total_min"], 30)
        w, wt = ai.brief(walk, self.INTENT_ARGS)
        d, dt = ai.brief(drive, self.INTENT_ARGS)
        self.assertEqual((wt["mode"], wt["lane"], dt["lane"]), ("rules", "walking", "driving"))
        self.assertIn("on foot", w["headline"])
        self.assertIn("on foot", w["summary"])
        self.assertTrue(any("long walk" in n for n in w["safety_notes"]))
        self.assertNotIn("on foot", d["headline"] + d["summary"])
        self.assertFalse(any("long walk" in n for n in d["safety_notes"]))

    def test_a_walker_with_nothing_in_range_is_pointed_at_the_driving_plan(self):
        patcher = mock.patch.dict(os.environ, {"AI_MODE": "off"})
        patcher.start()
        self.addCleanup(patcher.stop)
        with mock.patch.object(engine, "STORES", []), mock.patch.object(engine, "STORE_BY_ID", {}):
            plan = self.plan("walking")
        brief, _ = ai.brief(plan, self.INTENT_ARGS)
        self.assertIn("walking distance", brief["headline"])
        self.assertIn("driving plan", brief["summary"])

    def test_the_substitutes_agent_is_told_when_the_person_is_on_foot(self):
        fake = FakeBedrock([{"alternatives": [{"for_item": "water_bottled", "options": []}]}])
        ai.set_client(fake)
        gaps = [{"item_id": "water_bottled", "label": "water", "priority": 1, "why": ""}]
        _, trace = ai.alternatives(gaps, {"water_filter": {"dist_km": 1.0}}, {"disaster": {}, "household": {}}, "walking")
        self.assertEqual((trace["agent"], trace["lane"]), ("alternatives", "walking"))
        self.assertIn('"travel_mode": "walking"', fake.calls[0]["messages"][0]["content"][0]["text"])
        self.assertIn("light and easy to carry", fake.calls[0]["system"][0]["text"])

    def test_the_two_lanes_agents_are_inside_bedrock_at_the_same_time(self):
        """A barrier that only opens when both lanes' briefing agents are mid-call. If anything
        made the lanes take turns (a lock, a shared queue) the barrier would time out and the
        lanes would fall back to rules, which this test would catch."""
        import handler
        arrive = threading.Barrier(2, timeout=3)
        crossed = []                                               # threads that met the other lane at the barrier

        class Concurrent:
            def converse(self, **kw):
                if "intake analyst" in kw["system"][0]["text"]:
                    text = json.dumps({"needs": [{"item_id": "food_canned", "label": "canned food", "priority": 2}]})
                else:
                    arrive.wait()                              # returns only when the other lane is also in here
                    crossed.append(threading.get_ident())
                    text = json.dumps({"headline": "Go.", "steps": []})
                return {"output": {"message": {"role": "assistant", "content": [{"text": text}]}},
                        "usage": {"inputTokens": 1, "outputTokens": 1}}

        ai.set_client(Concurrent())
        body = {"text": "canned food please", "lat": 37.3382, "lng": -121.8863, "radius_km": 8, "local_minutes": 600,
                "modes": ["driving", "walking"]}
        for mode in ("driving", "walking"):                        # no substitutes needed: one briefing call per lane
            self.assertEqual(handler.api_search(dict(body, mode=mode, needs=[{"item_id": "food_canned"}]))["gaps"], [], mode)
        started = time.monotonic()
        out = handler.api_plan(body)
        self.assertLess(time.monotonic() - started, 2.5, "the lanes waited for each other's timeout instead of overlapping")
        self.assertEqual(len(crossed), 2)
        self.assertEqual(len(set(crossed)), 2, "both briefing agents must be inside Bedrock on different threads at once")
        traces = [t for t in out["traces"] if t["stage"] == "brief"]
        self.assertEqual({t["lane"] for t in traces}, {"driving", "walking"})
        self.assertEqual({t["mode"] for t in traces}, {"bedrock"}, [t.get("reason") for t in traces])
        self.assertEqual([t["fell_back_from"] for t in traces], [[], []])      # first model answered, no quiet fallback
        self.assertEqual(set(out["lanes"]), {"driving", "walking"})
        self.assertEqual(out["lanes"]["walking"]["plan"]["mode"], "walking")
        self.assertEqual(out["plan"]["mode"], "driving")           # the original keys are the first lane


class AssistantTests(Base):
    SITUATION = {"local_time": "10:00 AM", "selected_mode": "walking",
                 "plans": {"driving": {"stops": [{"store": "Alder Pharmacy", "minutes_to_get_there": 4,
                                                  "arrive": "10:04 AM", "hours": "until 9:00 PM"}],
                                       "total_minutes_with_shopping": 20},
                           "walking": {"stops": [], "total_minutes_with_shopping": 0}}}

    def test_the_model_is_given_the_situation_and_the_question(self):
        fake = FakeBedrock(["Walking will not work: nothing is in range. Driving takes 20 minutes."])
        ai.set_client(fake)
        ans, trace = ai.assistant("Can I walk instead?", self.SITUATION, "SUPPLYLINE PLAN text")
        prompt = fake.calls[0]["messages"][0]["content"][0]["text"]
        self.assertTrue(prompt.startswith("SITUATION\n"))
        self.assertIn('"selected_mode":"walking"', prompt)
        self.assertIn("Alder Pharmacy", prompt)
        self.assertIn("PLAN TEXT\nSUPPLYLINE PLAN text", prompt)
        self.assertTrue(prompt.endswith("QUESTION\nCan I walk instead?"))
        self.assertEqual((trace["agent"], trace["mode"]), ("followup", "bedrock"))
        self.assertIn("Use ONLY SITUATION", fake.calls[0]["system"][0]["text"])

    def test_with_no_situation_it_is_the_original_follow_up(self):
        fake = FakeBedrock(["Yes."])
        ai.set_client(fake)
        ai.followup("Can the dog come?", "PLAN text")
        prompt = fake.calls[0]["messages"][0]["content"][0]["text"]
        self.assertNotIn("SITUATION", prompt)
        self.assertIn("PLAN TEXT\nPLAN text", prompt)

    def test_without_a_model_it_still_answers_from_the_plans(self):
        patcher = mock.patch.dict(os.environ, {"AI_MODE": "off"})
        patcher.start()
        self.addCleanup(patcher.stop)
        ans, trace = ai.assistant("Will the pharmacy still be open?", self.SITUATION)
        self.assertEqual(trace["mode"], "rules")
        self.assertIn("Driving: 1 stop", ans)
        self.assertIn("Walking (shown): no open store within reach", ans)
        self.assertIn("Driving arrivals: Alder Pharmacy about 10:04 AM (until 9:00 PM)", ans)   # the question was about hours
        self.assertIn("not reachable", ans)
        plain, _ = ai.assistant("Is it ok?", None)
        self.assertTrue(plain.startswith("The AI model is not reachable right now"))

    def test_a_danger_in_the_question_always_leads_with_911_even_if_the_model_forgets(self):
        ai.set_client(FakeBedrock(["Walk to the pharmacy, it takes 20 minutes."]))
        ans, _ = ai.assistant("My dad can't breathe, should we walk to the pharmacy?", self.SITUATION)
        self.assertTrue(ans.startswith("Call 911 now"), ans)
        ai.set_client(FakeBedrock(["Call 911. Then stay with him."]))
        ans, _ = ai.assistant("My dad can't breathe", self.SITUATION)
        self.assertTrue(ans.startswith("Call 911."), ans)                        # the model already did: no second warning


if __name__ == "__main__":
    unittest.main()
