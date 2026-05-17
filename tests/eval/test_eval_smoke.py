"""
Smoke tests for the eval harness itself. These don't call Azure OpenAI —
they just validate the framework: golden set is parseable, fixtures install
cleanly, and the scoring function behaves as expected.

The real eval runs via tests/eval/run_eval.py and the eval.yml workflow.
"""
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
GOLDEN = ROOT / "tests" / "eval" / "golden_set.json"


def test_golden_set_loads():
    data = json.loads(GOLDEN.read_text())
    assert data["version"] == 1
    assert len(data["cases"]) >= 5, "Need at least 5 golden cases"


def test_golden_set_schema():
    data = json.loads(GOLDEN.read_text())
    seen_ids = set()
    for case in data["cases"]:
        for required in ("id", "description", "payload", "expected"):
            assert required in case, f"case missing {required}"
        assert case["id"] not in seen_ids, f"duplicate id: {case['id']}"
        seen_ids.add(case["id"])
        assert "account_number" in case["payload"]
        assert "amount" in case["payload"]
        assert "decision_in" in case["expected"]
        assert all(d in ("approve", "reject", "escalate") for d in case["expected"]["decision_in"])


def test_fixtures_install_and_lookup():
    from tests.eval import fixtures
    fixtures.install()

    from handlers.shared.cosmos import get_customer
    from handlers.shared.velocity import query_velocity

    steven = get_customer("4645120421")
    assert steven and steven["customer_name"] == "Steven Wilson"
    assert get_customer("nope") is None

    v = query_velocity("7857221324")
    assert v["count"] == 4 and v["near_ctr"] == 4


def test_scoring_matches_expectations():
    from tests.eval.run_eval import _score_case
    result = {
        "decision": "escalate",
        "fraud_pattern": "structuring",
        "tool_calls_made": [{"tool": "customer_lookup"}, {"tool": "velocity_check"}],
        "fraud_indicators": ["amount_near_ctr_threshold"],
        "reasoning": "Four prior near-CTR checks suggest structuring.",
    }
    expected = {
        "decision_in": ["escalate"],
        "fraud_pattern_in": ["structuring"],
        "must_call_tools": ["customer_lookup", "velocity_check"],
        "indicator_keywords": ["structuring", "ctr"],
    }
    s = _score_case(result, expected)
    assert s["decision_match"]
    assert s["pattern_match"]
    assert s["tools_match"]
    assert s["keyword_match"]
    assert s["overall_pass"]


def test_scoring_flags_missing_tool():
    from tests.eval.run_eval import _score_case
    result = {
        "decision": "escalate",
        "fraud_pattern": "structuring",
        "tool_calls_made": [{"tool": "customer_lookup"}],
        "fraud_indicators": [],
        "reasoning": "",
    }
    expected = {
        "decision_in": ["escalate"],
        "fraud_pattern_in": ["structuring"],
        "must_call_tools": ["customer_lookup", "velocity_check"],
        "indicator_keywords": [],
    }
    s = _score_case(result, expected)
    assert s["decision_match"]
    assert not s["tools_match"]
    assert s["missing_tools"] == ["velocity_check"]
    assert not s["overall_pass"]
