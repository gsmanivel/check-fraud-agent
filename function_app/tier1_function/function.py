import os
import json
import time
import logging
import azure.functions as func
from datetime import datetime, timezone, timedelta
import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from shared.models import (
    FraudDecision, Tier1Result,
    get_customer, upsert_check,
    write_audit_log, enqueue_message
)

logger = logging.getLogger(__name__)

app = func.FunctionApp()

APPROVE_THRESHOLD = int(os.environ.get("TIER1_APPROVE_THRESHOLD", "75"))
REJECT_THRESHOLD  = int(os.environ.get("TIER1_REJECT_THRESHOLD", "25"))


@app.service_bus_queue_trigger(
    arg_name="msg",
    queue_name="%TIER1_QUEUE_NAME%",
    connection="SERVICE_BUS_CONNECTION_STRING"
)
def tier1_function(msg: func.ServiceBusMessage):
    """
    Plain Azure Function triggered by Service Bus.
    Runs 4 fraud checks sequentially, scores risk, makes decision.
    Target: complete in < 200ms.
    """
    start = time.time()
    payload = json.loads(msg.get_body().decode("utf-8"))
    check_id = payload.get("id")
    logger.info(f"Tier 1 processing check: {check_id}")

    fraud_indicators = []
    checks_run = {}
    risk_score = 0

    try:
        # ── Check 1: MICR & field validation ─────────────────────────────
        micr_result = _check_micr_and_fields(payload)
        checks_run["micr_validation"] = micr_result
        risk_score += micr_result["risk_contribution"]
        fraud_indicators.extend(micr_result["indicators"])

        # ── Check 2: Amount validation ────────────────────────────────────
        amount_result = _check_amount(payload)
        checks_run["amount_validation"] = amount_result
        risk_score += amount_result["risk_contribution"]
        fraud_indicators.extend(amount_result["indicators"])

        # ── Check 3: Account status ───────────────────────────────────────
        account_result = _check_account(payload)
        checks_run["account_check"] = account_result
        risk_score += account_result["risk_contribution"]
        fraud_indicators.extend(account_result["indicators"])

        # ── Check 4: Velocity ─────────────────────────────────────────────
        velocity_result = _check_velocity(payload)
        checks_run["velocity_check"] = velocity_result
        risk_score += velocity_result["risk_contribution"]
        fraud_indicators.extend(velocity_result["indicators"])

        # ── Confidence gate ───────────────────────────────────────────────
        risk_score = min(risk_score, 100)
        decision = _confidence_gate(risk_score)

        processing_time_ms = int((time.time() - start) * 1000)
        logger.info(f"Check {check_id} | Score: {risk_score} | Decision: {decision} | {processing_time_ms}ms")

        # ── Update Cosmos DB ──────────────────────────────────────────────
        payload["risk_score"] = risk_score
        payload["fraud_indicators"] = fraud_indicators
        payload["processing_tier"] = "tier1"
        payload["fraud_decision"] = decision
        payload["status"] = decision if decision != FraudDecision.ESCALATE else "escalated"
        upsert_check(payload)

        # ── Write audit log ───────────────────────────────────────────────
        write_audit_log(check_id, "tier1", decision, {
            "risk_score": risk_score,
            "checks_run": checks_run,
            "fraud_indicators": fraud_indicators,
            "processing_time_ms": processing_time_ms
        })

        # ── Escalate to Tier 2 if needed ──────────────────────────────────
        if decision == FraudDecision.ESCALATE:
            payload["tier1_risk_score"] = risk_score
            payload["tier1_indicators"] = fraud_indicators
            enqueue_message(os.environ["TIER2_QUEUE_NAME"], payload)
            logger.info(f"Check {check_id} escalated to Tier 2")

        msg.complete()

    except Exception as e:
        logger.error(f"Tier 1 failed for check {check_id}: {str(e)}", exc_info=True)
        msg.abandon()
        raise


# ── Check implementations ─────────────────────────────────────────────────────

def _check_micr_and_fields(payload: dict) -> dict:
    """
    Validates MICR line integrity and field consistency.
    A real check image has these embedded — Doc Intelligence extracts them.
    """
    indicators = []
    risk = 0
    ef = payload.get("extracted_fields", {})

    if not ef.get("micr_valid", True):
        indicators.append("invalid_micr_checksum")
        risk += 40

    if ef.get("amount_mismatch", False):
        indicators.append("amount_words_numeric_mismatch")
        risk += 50
        mismatch_type = ef.get("mismatch_type", "amount_altered")
        indicators.append(mismatch_type)

    if not ef.get("payee_match", True):
        indicators.append("payee_name_mismatch")
        risk += 35

    if ef.get("alteration_detected", False):
        indicators.append("document_intelligence_alteration_flag")
        risk += 45

    if not ef.get("signature_present", True):
        indicators.append("missing_signature")
        risk += 20

    if ef.get("raw_ocr_confidence", 1.0) < 0.7:
        indicators.append("low_ocr_confidence")
        risk += 10

    return {
        "passed": risk == 0,
        "risk_contribution": risk,
        "indicators": indicators
    }


def _check_amount(payload: dict) -> dict:
    """
    Checks amount against account limits and suspicious thresholds.
    """
    indicators = []
    risk = 0
    amount = payload.get("amount", 0)

    # Structuring threshold — just below $10k CTR reporting requirement
    if 8500 <= amount <= 9999:
        indicators.append("amount_near_ctr_threshold")
        risk += 20

    # Very large check
    if amount > 50000:
        indicators.append("large_amount_check")
        risk += 15

    # Zero or negative amount
    if amount <= 0:
        indicators.append("invalid_amount")
        risk += 50

    return {
        "passed": risk == 0,
        "risk_contribution": risk,
        "indicators": indicators
    }


def _check_account(payload: dict) -> dict:
    """
    Looks up customer in Cosmos DB and validates account status.
    """
    indicators = []
    risk = 0
    account_number = payload.get("account_number", "")

    customer = get_customer(account_number)

    if not customer:
        indicators.append("account_not_found")
        risk += 60
        return {"passed": False, "risk_contribution": risk, "indicators": indicators, "customer": None}

    if customer.get("account_status") != "active":
        indicators.append("account_not_active")
        risk += 50

    if customer.get("synthetic_identity_risk", False):
        indicators.append("synthetic_identity_flag_on_account")
        risk += 40

    if not customer.get("kyc_verified", True):
        indicators.append("kyc_not_verified")
        risk += 30

    # Account too new for large amounts
    account_age_days = _get_account_age_days(customer.get("opened_date", ""))
    amount = payload.get("amount", 0)
    if account_age_days < 90 and amount > 5000:
        indicators.append("new_account_large_amount")
        risk += 25

    if customer.get("flags"):
        for flag in customer["flags"]:
            indicators.append(f"customer_flag_{flag}")
            risk += 20

    return {
        "passed": risk == 0,
        "risk_contribution": risk,
        "indicators": indicators,
        "account_age_days": account_age_days
    }


def _check_velocity(payload: dict) -> dict:
    """
    Checks transaction frequency for this account in last 24 hours.
    Queries Cosmos DB for recent checks from same account.
    """
    indicators = []
    risk = 0
    account_number = payload.get("account_number", "")

    from shared.models import get_cosmos_container
    container = get_cosmos_container(os.environ["COSMOS_CHECKS_CONTAINER"])
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()

    query = f"""
        SELECT VALUE COUNT(1) FROM c
        WHERE c.account_number = '{account_number}'
        AND c.submission_date >= '{cutoff}'
        AND c.id != '{payload.get("id", "")}'
    """
    results = list(container.query_items(query=query, enable_cross_partition_query=True))
    count_24h = results[0] if results else 0

    if count_24h >= 5:
        indicators.append("high_velocity_5plus_checks_24h")
        risk += 35
    elif count_24h >= 3:
        indicators.append("elevated_velocity_3plus_checks_24h")
        risk += 15

    # Check total amount in 24h window — structuring detection seed
    amount_query = f"""
        SELECT VALUE SUM(c.amount) FROM c
        WHERE c.account_number = '{account_number}'
        AND c.submission_date >= '{cutoff}'
        AND c.id != '{payload.get("id", "")}'
    """
    amount_results = list(container.query_items(query=amount_query, enable_cross_partition_query=True))
    total_24h = (amount_results[0] or 0) + payload.get("amount", 0)

    if total_24h >= 9000:
        indicators.append("cumulative_amount_near_ctr_threshold_24h")
        risk += 30

    return {
        "passed": risk == 0,
        "risk_contribution": risk,
        "indicators": indicators,
        "checks_in_24h": count_24h,
        "total_amount_24h": total_24h
    }


# ── Helpers ───────────────────────────────────────────────────────────────────

def _confidence_gate(risk_score: int) -> str:
    """
    Maps risk score to a decision.
    Score 0-25   → Approve  (low risk, Tier 1 handles it)
    Score 26-74  → Escalate (uncertain, send to Tier 2 agent)
    Score 75-100 → Reject   (high confidence fraud, Tier 1 rejects)
    """
    if risk_score <= REJECT_THRESHOLD:
        return FraudDecision.APPROVE
    elif risk_score >= APPROVE_THRESHOLD:
        return FraudDecision.REJECT
    else:
        return FraudDecision.ESCALATE


def _get_account_age_days(opened_date: str) -> int:
    if not opened_date:
        return 0
    try:
        opened = datetime.fromisoformat(opened_date.replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - opened).days
    except Exception:
        return 0
