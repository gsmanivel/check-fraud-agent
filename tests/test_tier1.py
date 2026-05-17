import pytest
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from handlers.tier1 import _t1_micr, _t1_amount
from handlers.shared.utils import confidence_gate
from handlers.shared.models import CheckPayload


def _make_payload(data: dict) -> CheckPayload:
    base = {"id": "test_id", "account_number": "12345", "amount": 100.0}
    base.update(data)
    return CheckPayload(**base)


# ── MICR & field tests ────────────────────────────────────────────────────────

def test_clean_check_passes_micr():
    payload = {
        "extracted_fields": {
            "micr_valid": True,
            "amount_mismatch": False,
            "payee_match": True,
            "alteration_detected": False,
            "signature_present": True,
            "raw_ocr_confidence": 0.95
        }
    }
    result = _t1_micr(_make_payload(payload))
    assert result["passed"] is True
    assert result["risk_contribution"] == 0
    assert result["indicators"] == []


def test_altered_check_detected():
    payload = {
        "extracted_fields": {
            "micr_valid": True,
            "amount_mismatch": True,
            "payee_match": True,
            "alteration_detected": True,
            "signature_present": True,
            "raw_ocr_confidence": 0.90
        }
    }
    result = _t1_micr(_make_payload(payload))
    assert result["passed"] is False
    assert "amount_words_numeric_mismatch" in result["indicators"]
    assert "alteration_detected" in result["indicators"]
    assert result["risk_contribution"] >= 90


def test_missing_signature_adds_risk():
    payload = {
        "extracted_fields": {
            "micr_valid": True,
            "amount_mismatch": False,
            "payee_match": True,
            "alteration_detected": False,
            "signature_present": False,
            "raw_ocr_confidence": 0.95
        }
    }
    result = _t1_micr(_make_payload(payload))
    assert "missing_signature" in result["indicators"]
    assert result["risk_contribution"] == 20


def test_invalid_micr_adds_high_risk():
    payload = {
        "extracted_fields": {
            "micr_valid": False,
            "amount_mismatch": False,
            "payee_match": True,
            "alteration_detected": False,
            "signature_present": True,
            "raw_ocr_confidence": 0.95
        }
    }
    result = _t1_micr(_make_payload(payload))
    assert "invalid_micr_checksum" in result["indicators"]
    assert result["risk_contribution"] == 40


# ── Amount tests ──────────────────────────────────────────────────────────────

def test_structuring_amount_flagged():
    payload = {"amount": 9500.00}
    result = _t1_amount(_make_payload(payload))
    assert "amount_near_ctr_threshold" in result["indicators"]
    assert result["risk_contribution"] >= 20


def test_normal_amount_passes():
    payload = {"amount": 1200.00}
    result = _t1_amount(_make_payload(payload))
    assert result["passed"] is True
    assert result["risk_contribution"] == 0


def test_zero_amount_rejected():
    payload = {"amount": 0}
    result = _t1_amount(_make_payload(payload))
    assert "invalid_amount" in result["indicators"]
    assert result["risk_contribution"] == 50


def test_large_amount_flagged():
    payload = {"amount": 75000.00}
    result = _t1_amount(_make_payload(payload))
    assert "large_amount_check" in result["indicators"]


# ── Confidence gate tests ─────────────────────────────────────────────────────

def test_low_risk_score_approves():
    assert confidence_gate(10) == "approve"


def test_mid_risk_score_escalates():
    assert confidence_gate(50) == "escalate"


def test_high_risk_score_rejects():
    assert confidence_gate(80) == "reject"


def test_boundary_approve_threshold():
    assert confidence_gate(25) == "approve"
    assert confidence_gate(26) == "escalate"


def test_boundary_reject_threshold():
    assert confidence_gate(74) == "escalate"
    assert confidence_gate(75) == "reject"
