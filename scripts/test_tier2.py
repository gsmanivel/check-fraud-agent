import json, sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

settings = json.load(open("local.settings.json"))["Values"]
for k, v in settings.items():
    os.environ[k] = v

from handlers.tier2 import _run_agent
import time

payload = {
    "id": "4ba64a3e-b5c0-4b50-9dc8-09fff689017e",
    "account_number": "1478163327",
    "amount": 7500.00,
    "payee_name": "Jane Doe",
    "bank_name": "Test Bank",
    "submission_date": "2026-05-12T00:00:00+00:00",
    "tier1_risk_score": 60,
    "tier1_indicators": ["missing_signature", "low_ocr_confidence", "cumulative_amount_near_ctr_24h"],
    "extracted_fields": {
        "micr_valid": True,
        "amount_mismatch": False,
        "signature_present": False,
        "raw_ocr_confidence": 0.65
    }
}

try:
    result = _run_agent(payload, time.time())
    print("Decision  :", result.get("decision"))
    print("Risk score:", result.get("risk_score"))
    print("Pattern   :", result.get("fraud_pattern"))
    print("Reasoning :", result.get("reasoning"))
except Exception as e:
    print("ERROR:", e)
