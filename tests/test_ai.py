"""Tests for the Bedrock layer using a scripted fake bedrock-runtime client.

These verify OUR code around Bedrock (request shape, JSON validation, repair
retry, model fallback chain, safety enforcement, rules fallback). They do not
call AWS; see scripts/bedrock_check.py for a real end-to-end call.
"""
import json
import os
import sys
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

    def test_grill_tank_is_never_offered_for_a_camp_canister(self):
        pool = {"propane_20lb": {"dist_km": 0.5}, "butane_canister": {"dist_km": 2.0}}
        reply = {"alternatives": [{"for_item": "propane_1lb", "options": [
            {"item_id": "propane_20lb", "fit": "strong", "why": "", "how_to_use": "", "caution": ""},
            {"item_id": "butane_canister", "fit": "partial", "why": "", "how_to_use": "", "caution": ""}]}]}
        ai.set_client(FakeBedrock([reply]))
        alts, _ = ai.alternatives([{"item_id": "propane_1lb", "label": "propane", "priority": 2, "why": ""}], pool, {})
        self.assertEqual([o["item_id"] for o in alts[0]["options"]], ["butane_canister"])
        self.assertIn("butane stoves only", alts[0]["options"][0]["caution"])

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

    def test_prompt_never_offers_prescription_items_as_candidates(self):
        fake = FakeBedrock([{"alternatives": [{"for_item": "water_bottled", "options": []}]}])
        ai.set_client(fake)
        ai.alternatives([{"item_id": "water_bottled", "label": "water", "priority": 1, "why": ""}], self.POOL, {})
        prompt = fake.calls[0]["messages"][0]["content"][0]["text"]
        avail = prompt.split("AVAILABLE")[1]
        self.assertNotIn('"item_id": "insulin"', avail)
        self.assertIn('"item_id": "water_filter"', avail)

    def test_fallback_prefers_tag_matches_and_keeps_cautions(self):
        os.environ["AI_MODE"] = "off"
        try:
            ai.set_client(FakeBedrock([]))
            pool = {k: {"dist_km": 2.0 + i} for i, k in enumerate(["butane_canister", "camp_stove_propane", "canned_fuel", "headlamp"])}
            alts, trace = ai.alternatives([{"item_id": "propane_1lb", "label": "propane", "priority": 2, "why": ""}], pool, {})
        finally:
            os.environ["AI_MODE"] = "auto"
        self.assertEqual(trace["mode"], "rules")
        ids = [o["item_id"] for o in alts[0]["options"]]
        self.assertIn("butane_canister", ids)
        butane = next(o for o in alts[0]["options"] if o["item_id"] == "butane_canister")
        self.assertIn("butane stoves only", butane["caution"])
        self.assertNotIn("headlamp", ids)


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


if __name__ == "__main__":
    unittest.main()
