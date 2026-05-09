import pytest
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from function_app.tier1_function.function import (
    _check_micr_and_fields,
    _check_amount,
    _confidence_gate
)
from shared.models import FraudDecision


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
    result = _check_micr_and_fields(payload)
    assert result["passed"] is True
    assert result["risk_contribution"] == 0
    assert result["indicators"] == []


def test_altered_check_detected():
    payload = {
        "extracted_fields": {
            "micr_valid": True,
            "amount_mismatch": True,
            "mismatch_type": "amount_altered",
            "payee_match": True,
            "alteration_detected": True,
            "signature_present": True,
            "raw_ocr_confidence": 0.90
        }
    }
    result = _check_micr_and_fields(payload)
    assert result["passed"] is False
    assert "amount_words_numeric_mismatch" in result["indicators"]
    assert "document_intelligence_alteration_flag" in result["indicators"]
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
    result = _check_micr_and_fields(payload)
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
    result = _check_micr_and_fields(payload)
    assert "invalid_micr_checksum" in result["indicators"]
    assert result["risk_contribution"] == 40


# ── Amount tests ──────────────────────────────────────────────────────────────

def test_structuring_amount_flagged():
    payload = {"amount": 9500.00}
    result = _check_amount(payload)
    assert "amount_near_ctr_threshold" in result["indicators"]
    assert result["risk_contribution"] >= 20


def test_normal_amount_passes():
    payload = {"amount": 1200.00}
    result = _check_amount(payload)
    assert result["passed"] is True
    assert result["risk_contribution"] == 0


def test_zero_amount_rejected():
    payload = {"amount": 0}
    result = _check_amount(payload)
    assert "invalid_amount" in result["indicators"]
    assert result["risk_contribution"] == 50


def test_large_amount_flagged():
    payload = {"amount": 75000.00}
    result = _check_amount(payload)
    assert "large_amount_check" in result["indicators"]


# ── Confidence gate tests ─────────────────────────────────────────────────────

def test_low_risk_score_approves():
    assert _confidence_gate(10) == FraudDecision.APPROVE


def test_mid_risk_score_escalates():
    assert _confidence_gate(50) == FraudDecision.ESCALATE


def test_high_risk_score_rejects():
    assert _confidence_gate(80) == FraudDecision.REJECT


def test_boundary_approve_threshold():
    assert _confidence_gate(25) == FraudDecision.APPROVE
    assert _confidence_gate(26) == FraudDecision.ESCALATE


def test_boundary_reject_threshold():
    assert _confidence_gate(74) == FraudDecision.ESCALATE
    assert _confidence_gate(75) == FraudDecision.REJECT
