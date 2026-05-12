import os, logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

APPROVE_THRESHOLD = int(os.environ.get("TIER1_APPROVE_THRESHOLD", "75"))
REJECT_THRESHOLD  = int(os.environ.get("TIER1_REJECT_THRESHOLD", "25"))


def validate_micr(routing_number: str) -> bool:
    if not routing_number or len(routing_number) != 9:
        return False
    try:
        d = [int(c) for c in routing_number]
        return (3*(d[0]+d[3]+d[6]) + 7*(d[1]+d[4]+d[7]) + (d[2]+d[5]+d[8])) % 10 == 0
    except Exception:
        return False


def confidence_gate(risk_score: int) -> str:
    if risk_score <= REJECT_THRESHOLD:    return "approve"
    elif risk_score >= APPROVE_THRESHOLD: return "reject"
    else:                                 return "escalate"


def get_account_age_days(opened_date: str) -> int:
    if not opened_date:
        return 0
    try:
        opened = datetime.fromisoformat(opened_date.replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - opened).days
    except Exception:
        return 0
