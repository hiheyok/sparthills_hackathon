"""Validate our AWS calls against the real AWS service models (no network, no account).

* Bedrock: a real boto3 'bedrock-runtime' client + botocore Stubber. Stubber checks
  every parameter we send to Converse against the official API model and checks the
  canned response against the output model, so a misspelled or mis-nested field fails here.
* DynamoDB: moto (in-process fake) exercises the real boto3 code path of the report store.
"""
import json
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")

try:
    import boto3
    from botocore.stub import Stubber
    HAVE_BOTO = True
except ImportError:  # pragma: no cover
    HAVE_BOTO = False

try:
    from moto import mock_aws
    HAVE_MOTO = True
except ImportError:  # pragma: no cover
    HAVE_MOTO = False

import ai  # noqa: E402

INTENT = {"disaster": {"type": "storm", "summary": "Outage."}, "household": {"people": 2},
          "urgent_safety": {"flag": False, "reason": ""},
          "needs": [{"item_id": "batt_d", "label": "D batteries", "qty": 2, "priority": 2, "inferred": False, "why": ""}],
          "unmapped": []}


def converse_response(text):
    return {"output": {"message": {"role": "assistant", "content": [{"text": text}]}},
            "stopReason": "end_turn", "usage": {"inputTokens": 321, "outputTokens": 45, "totalTokens": 366},
            "metrics": {"latencyMs": 812}}


@unittest.skipUnless(HAVE_BOTO, "boto3 not installed")
class BedrockRequestShape(unittest.TestCase):
    def setUp(self):
        os.environ["AI_MODE"] = "auto"
        os.environ.pop("BEDROCK_MODEL_IDS", None)
        os.environ.pop("GUARDRAIL_ID", None)
        self.client = boto3.client("bedrock-runtime", region_name="us-east-1")
        self.stub = Stubber(self.client)
        ai.set_client(self.client)

    def tearDown(self):
        os.environ.pop("GUARDRAIL_ID", None)
        ai.set_client(None)

    def test_converse_request_is_valid_for_every_stage(self):
        # Stubber raises ParamValidationError if any parameter is not in the real API model
        self.stub.add_response("converse", converse_response(json.dumps(INTENT)))
        self.stub.add_response("converse", converse_response(json.dumps(
            {"alternatives": [{"for_item": "batt_d", "options": []}]})))
        self.stub.add_response("converse", converse_response(json.dumps({"headline": "Go now.", "steps": []})))
        self.stub.add_response("converse", converse_response("Yes, that is fine."))
        with self.stub:
            intent, t1 = ai.understand("We need D batteries")
            self.assertEqual(t1["mode"], "bedrock")
            self.assertEqual(t1["tokens_in"], 321)
            alts, t2 = ai.alternatives([{"item_id": "batt_d", "label": "D batteries", "priority": 2, "why": ""}],
                                       {"headlamp": {"dist_km": 1.0}}, intent)
            self.assertEqual(t2["mode"], "bedrock")
            import engine
            plan = engine.build_plan(intent["needs"], [], 37.33, -121.88, 8, 600, {}, time.time())
            brief, t3 = ai.brief(plan, intent)
            self.assertEqual(t3["mode"], "bedrock")
            ans, t4 = ai.followup("Is that ok?", "PLAN")
            self.assertEqual(t4["mode"], "bedrock")
        self.stub.assert_no_pending_responses()

    def test_guardrail_config_is_valid(self):
        os.environ["GUARDRAIL_ID"] = "abc123def456"
        os.environ["GUARDRAIL_VERSION"] = "1"
        self.stub.add_response("converse", converse_response(json.dumps(INTENT)))
        with self.stub:
            _, trace = ai.understand("We need D batteries")
        self.assertEqual(trace["mode"], "bedrock")

    def test_real_client_errors_are_classified_and_fall_through(self):
        self.stub.add_client_error("converse", service_error_code="AccessDeniedException", http_status_code=403)
        self.stub.add_response("converse", converse_response(json.dumps(INTENT)))
        with self.stub:
            _, trace = ai.understand("We need D batteries")
        self.assertEqual(trace["model"], ai.model_chain()[1])
        self.assertEqual(trace["fell_back_from"], [ai.model_chain()[0]])

    def test_throttling_then_validation_then_success(self):
        self.stub.add_client_error("converse", service_error_code="ThrottlingException", http_status_code=429)
        self.stub.add_client_error("converse", service_error_code="ValidationException", http_status_code=400)
        self.stub.add_response("converse", converse_response(json.dumps(INTENT)))
        with self.stub:
            _, trace = ai.understand("We need D batteries")
        self.assertEqual(trace["model"], ai.model_chain()[2])

    def test_every_model_failing_degrades_to_rules_not_an_exception(self):
        for _ in ai.model_chain():
            self.stub.add_client_error("converse", service_error_code="AccessDeniedException", http_status_code=403)
        with self.stub:
            intent, trace = ai.understand("We need D batteries and water")
        self.assertEqual(trace["mode"], "rules")
        self.assertTrue(intent["needs"])


@unittest.skipUnless(HAVE_BOTO and HAVE_MOTO, "boto3/moto not installed")
class DynamoReportStore(unittest.TestCase):
    def test_roundtrip_ttl_and_expiry(self):
        import store
        with mock_aws():
            ddb = boto3.client("dynamodb", region_name="us-east-1")
            ddb.create_table(TableName="reports", BillingMode="PAY_PER_REQUEST",
                             AttributeDefinitions=[{"AttributeName": "store_id", "AttributeType": "S"},
                                                   {"AttributeName": "item_id", "AttributeType": "S"}],
                             KeySchema=[{"AttributeName": "store_id", "KeyType": "HASH"},
                                        {"AttributeName": "item_id", "KeyType": "RANGE"}])
            s = store.DynamoReportStore("reports")
            now = int(time.time())
            s.put("s01", "water_bottled", "in_stock", ts=now - 60)
            s.put("s02", "*", "closed", ts=now - 30)
            s.put("s03", "insulin", "out", ts=now - store.REPORT_TTL_S - 100)   # expired: filtered out
            got = s.all_recent(now)
            self.assertEqual(set(got), {("s01", "water_bottled"), ("s02", "*")})
            self.assertEqual(got[("s01", "water_bottled")], {"status": "in_stock", "ts": now - 60})
            item = ddb.get_item(TableName="reports", Key={"store_id": {"S": "s01"}, "item_id": {"S": "water_bottled"}})["Item"]
            self.assertEqual(int(item["ttl"]["N"]), now - 60 + store.REPORT_TTL_S)
            # a new write invalidates the read cache
            s.put("s04", "diapers", "low")
            self.assertIn(("s04", "diapers"), s.all_recent(int(time.time())))


if __name__ == "__main__":
    unittest.main()
