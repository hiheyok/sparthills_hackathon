"""Checks on infra/template.yaml that the SDK request-shape tests cannot make: IAM permissions.

The template uses CloudFormation tags (!If, !Sub), which the standard library cannot parse,
so these tests read the YAML blocks as text. They fail when the Lambda environment turns a
feature on without the role being allowed to use it.
"""
import inspect
import os
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import ai  # noqa: E402

TEMPLATE = (Path(__file__).resolve().parent.parent / "infra" / "template.yaml").read_text(encoding="utf-8")


def block(text, header):
    """The YAML block that starts at the first line containing `header`: that line and every
    following line indented deeper than it."""
    lines = text.splitlines()
    start = next(n for n, line in enumerate(lines) if header in line)
    indent = len(lines[start]) - len(lines[start].lstrip())
    out = [lines[start]]
    for line in lines[start + 1:]:
        if line.strip() and len(line) - len(line.lstrip()) <= indent:
            break
        out.append(line)
    return "\n".join(out)


class GuardrailPermission(unittest.TestCase):
    def test_a_configured_guardrail_is_authorized_on_exactly_that_guardrail(self):
        policy = block(TEMPLATE, "PolicyName: BedrockInvoke")
        grant = block(policy, "- !If")
        self.assertRegex(grant, r"-\s+HasGuardrail\b")                       # only when a guardrail is configured
        self.assertIn("Action: bedrock:ApplyGuardrail", grant)
        self.assertIn("Effect: Allow", grant)
        self.assertIn("!Sub 'arn:${AWS::Partition}:bedrock:${BedrockRegion}:${AWS::AccountId}:guardrail/${GuardrailId}'", grant)
        self.assertRegex(grant, r"-\s+!Ref AWS::NoValue")                    # no statement at all otherwise
        grants = re.findall(r"^\s*(?:-\s*|Action:\s*)bedrock:ApplyGuardrail\s*$", TEMPLATE, re.M)
        self.assertEqual(len(grants), 1, "granted once, never unconditionally or on '*'")

    def test_model_invocation_is_still_granted(self):
        policy = block(TEMPLATE, "PolicyName: BedrockInvoke")
        for action in ("bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"):
            self.assertIn(action, policy)

    def test_the_permission_and_the_environment_variable_share_one_condition(self):
        self.assertRegex(TEMPLATE, r"HasGuardrail:\s*!Not \[\s*!Equals \[\s*!Ref GuardrailId,\s*''\s*\]\s*\]")
        env = block(TEMPLATE, "Variables:")
        self.assertRegex(env, r"GUARDRAIL_ID:\s*!If \[HasGuardrail,\s*!Ref GuardrailId,\s*''\]")
        self.assertIn("GUARDRAIL_VERSION", env)

    def test_the_parameter_only_accepts_a_guardrail_id(self):
        param = block(TEMPLATE, "  GuardrailId:")
        pattern = re.search(r"AllowedPattern:\s*'([^']*)'", param).group(1)
        self.assertTrue(re.fullmatch(pattern, ""))                          # empty still means no guardrail
        self.assertTrue(re.fullmatch(pattern, "abc123def456"))
        for bad in ("arn:aws:bedrock:us-east-1:111122223333:guardrail/abc123", "abc123:1", "my guardrail", "abc/123", "*"):
            self.assertFalse(re.fullmatch(pattern, bad), bad)                # would build a wrong or wildcard IAM resource

    def test_code_that_attaches_a_guardrail_is_matched_by_the_permission(self):
        self.assertIn("guardrailConfig", inspect.getsource(ai._converse))
        self.assertIn("bedrock:ApplyGuardrail", TEMPLATE)


if __name__ == "__main__":
    unittest.main()
