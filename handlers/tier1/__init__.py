import os, json, time, logging
import azure.functions as func
import azure.durable_functions as df

from handlers.shared.cosmos import get_customer, upsert_check, write_audit_log
from handlers.shared.servicebus import enqueue_message
from handlers.shared.utils import confidence_gate, get_account_age_days
from handlers.shared.velocity import query_velocity

logger = logging.getLogger(__name__)
bp = df.Blueprint()


@bp.service_bus_queue_trigger(
    arg_name="msg",
    queue_name="%TIER1_QUEUE_NAME%",
    connection="SERVICE_BUS_CONNECTION_STRING"
)
def tier1_function(msg: func.ServiceBusMessage):
    start    = time.time()
    payload  = json.loads(msg.get_body().decode("utf-8"))
    check_id = payload.get("id")
    logger.info(f"Tier 1 processing: {check_id}")
    fraud_indicators = []
    checks_run       = {}
    risk_score       = 0
    try:
        for check_fn in [_t1_micr, _t1_amount, _t1_account, _t1_velocity]:
            result = check_fn(payload)
            checks_run[result["name"]] = result
            risk_score += result["risk_contribution"]
            fraud_indicators.extend(result["indicators"])
        risk_score = min(risk_score, 100)
        decision   = confidence_gate(risk_score)
        ms         = int((time.time() - start) * 1000)
        logger.info(f"Check {check_id} | Score:{risk_score} | {decision} | {ms}ms")
        payload.update({
            "risk_score":       risk_score,
            "fraud_indicators": fraud_indicators,
            "processing_tier":  "tier1",
            "fraud_decision":   decision,
            "status":           decision if decision != "escalate" else "escalated"
        })
        upsert_check(payload)
        write_audit_log(check_id, "tier1", decision, {"risk_score": risk_score, "ms": ms})
        if decision == "escalate":
            payload["tier1_risk_score"] = risk_score
            payload["tier1_indicators"] = fraud_indicators
            enqueue_message(os.environ["TIER2_QUEUE_NAME"], payload)
    except Exception as e:
        logger.error(f"Tier 1 failed {check_id}: {e}", exc_info=True)
        raise


def _t1_micr(payload: dict) -> dict:
    indicators, risk = [], 0
    ef = payload.get("extracted_fields", {})
    if not ef.get("micr_valid", True):           indicators.append("invalid_micr_checksum");         risk += 40
    if ef.get("amount_mismatch", False):          indicators.append("amount_words_numeric_mismatch"); risk += 50
    if not ef.get("payee_match", True):           indicators.append("payee_name_mismatch");           risk += 35
    if ef.get("alteration_detected", False):      indicators.append("alteration_detected");           risk += 45
    if not ef.get("signature_present", True):     indicators.append("missing_signature");             risk += 20
    if ef.get("raw_ocr_confidence", 1.0) < 0.7:  indicators.append("low_ocr_confidence");            risk += 10
    return {"name": "micr_validation", "passed": risk == 0, "risk_contribution": risk, "indicators": indicators}


def _t1_amount(payload: dict) -> dict:
    indicators, risk = [], 0
    amount = payload.get("amount", 0)
    if 8500 <= amount <= 9999:  indicators.append("amount_near_ctr_threshold"); risk += 20
    if amount > 50000:          indicators.append("large_amount_check");        risk += 15
    if amount <= 0:             indicators.append("invalid_amount");            risk += 50
    return {"name": "amount_validation", "passed": risk == 0, "risk_contribution": risk, "indicators": indicators}


def _t1_account(payload: dict) -> dict:
    indicators, risk = [], 0
    customer = get_customer(payload.get("account_number", ""))
    if not customer:
        return {"name": "account_check", "passed": False, "risk_contribution": 60, "indicators": ["account_not_found"]}
    if customer.get("account_status") != "active":        indicators.append("account_not_active");      risk += 50
    if customer.get("synthetic_identity_risk", False):    indicators.append("synthetic_identity_flag"); risk += 40
    if not customer.get("kyc_verified", True):            indicators.append("kyc_not_verified");        risk += 30
    age = get_account_age_days(customer.get("opened_date", ""))
    if age < 90 and payload.get("amount", 0) > 5000:     indicators.append("new_account_large_amount");risk += 25
    for flag in customer.get("flags", []):                indicators.append(f"flag_{flag}");            risk += 20
    return {"name": "account_check", "passed": risk == 0, "risk_contribution": risk, "indicators": indicators}


def _t1_velocity(payload: dict) -> dict:
    indicators, risk = [], 0
    data      = query_velocity(payload.get("account_number", ""), days_back=1, exclude_check_id=payload.get("id", ""))
    count_24h = data["count"]
    total_24h = data["total"] + payload.get("amount", 0)
    if count_24h >= 5:    indicators.append("high_velocity_5plus_24h");        risk += 35
    elif count_24h >= 3:  indicators.append("elevated_velocity_3plus_24h");    risk += 15
    if total_24h >= 9000: indicators.append("cumulative_amount_near_ctr_24h"); risk += 30
    return {"name": "velocity_check", "passed": risk == 0, "risk_contribution": risk, "indicators": indicators, "count_24h": count_24h, "total_24h": total_24h}
