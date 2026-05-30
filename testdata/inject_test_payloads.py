"""
Inject test payloads directly to the Tier 1 Service Bus queue,
bypassing blob trigger and Document Intelligence.

Run after seeding Cosmos and starting the Function App:
    python testData/inject_test_payloads.py
"""
import json
import os
import sys
import uuid
from datetime import datetime, timezone

# Load local.settings.json into env
_settings_path = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "local.settings.json",
)
with open(_settings_path) as _f:
    _values = json.load(_f).get("Values", {})
    for _k, _v in _values.items():
        os.environ.setdefault(_k, str(_v))

from azure.servicebus import ServiceBusClient, ServiceBusMessage


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def build_payload(
    account_number: str,
    routing_number: str,
    amount: float,
    payee_name: str,
    bank_name: str,
    customer_name: str,
    micr_valid: bool = True,
    amount_mismatch: bool = False,
    signature_present: bool = True,
    alteration_detected: bool = False,
    raw_ocr_confidence: float = 0.95,
    amount_words: str = "",
    memo: str = "",
    check_number: str = "1001",
) -> dict:
    check_id = str(uuid.uuid4())
    return {
        "id":              check_id,
        "check_number":    check_number,
        "account_number":  account_number,
        "routing_number":  routing_number,
        "customer_name":   customer_name,
        "payee_name":      payee_name,
        "amount":          amount,
        "memo":            memo,
        "issue_date":      now_iso(),
        "submission_date": now_iso(),
        "bank_name":       bank_name,
        "check_image_url": f"https://test/{check_id}.jpg",
        "blob_path":       f"test/{check_id}.jpg",
        "extracted_fields": {
            "amount_numeric":      amount,
            "amount_words":        amount_words or f"{amount} dollars",
            "payee_name":          payee_name,
            "payee_match":         True,
            "micr_valid":          micr_valid,
            "signature_present":   signature_present,
            "amount_mismatch":     amount_mismatch,
            "alteration_detected": alteration_detected,
            "raw_ocr_confidence":  raw_ocr_confidence,
            "account_number":      account_number,
            "routing_number":      routing_number,
            "bank_name":           bank_name,
            "memo":                memo,
        },
        "status":     "pending",
        "created_at": now_iso(),
        "updated_at": now_iso(),
    }


# Test scenarios
SCENARIOS = [
    {
        "name": "SC1 — Auto Approve (clean check)",
        "payload": build_payload(
            account_number="1478163327",
            routing_number="021000021",
            amount=875.00,
            payee_name="Melissa Moore",
            bank_name="JPMORGAN CHASE BANK",
            customer_name="David Mitchell",
            amount_words="Eight Hundred Seventy-Five and 00/100",
            memo="Rent - May 2026",
            check_number="1340",
        ),
    },
    {
        "name": "SC2 — Auto Reject (invalid MICR + amount mismatch + alteration)",
        "payload": build_payload(
            account_number="1478163327",
            routing_number="111111111",
            amount=14000.00,
            payee_name="Quick Cash LLC",
            bank_name="BANK OF AMERICA",
            customer_name="David Mitchell",
            micr_valid=False,
            amount_mismatch=True,
            alteration_detected=True,
            raw_ocr_confidence=0.45,
            amount_words="Eight Thousand",
            memo="Services",
            check_number="4849",
        ),
    },
    {
        "name": "SC3 — Escalate to Tier 2 (low OCR + unknown payee)",
        "payload": build_payload(
            account_number="1478163327",
            routing_number="021000021",
            amount=3200.00,
            payee_name="Pacific Northwest Consulting LLC",
            bank_name="JPMORGAN CHASE BANK",
            customer_name="David Mitchell",
            raw_ocr_confidence=0.65,
            amount_words="Three Thousand Two Hundred and 00/100",
            memo="Consulting Invoice #847",
            check_number="1042",
        ),
    },
    {
        "name": "SC4 — Structuring (just below CTR threshold)",
        "payload": build_payload(
            account_number="1478163327",
            routing_number="026009593",
            amount=9500.00,
            payee_name="Global Co",
            bank_name="CITIBANK",
            customer_name="David Mitchell",
            amount_words="Nine Thousand Five Hundred and 00/100",
            memo="Invoice",
            check_number="6720",
        ),
    },
    {
        "name": "SC5 — Altered Check (heavy smudge)",
        "payload": build_payload(
            account_number="1478163327",
            routing_number="021000021",
            amount=12000.00,
            payee_name="Miller Supplies Inc",
            bank_name="US BANK",
            customer_name="David Mitchell",
            alteration_detected=True,
            raw_ocr_confidence=0.55,
            amount_words="Twelve Thousand and 00/100",
            memo="Equipment",
            check_number="2200",
        ),
    },
    {
        "name": "SC6 — Synthetic Identity (fraud from customer record)",
        "payload": build_payload(
            account_number="1478163327",
            routing_number="026009593",
            amount=2500.00,
            payee_name="Cash",
            bank_name="TD BANK",
            customer_name="Anthony Perez",
            amount_words="Two Thousand Five Hundred and 00/100",
            check_number="1001",
        ),
    },
    {
        "name": "SC7 — Money Mule (amount mismatch)",
        "payload": build_payload(
            account_number="1478163327",
            routing_number="021000021",
            amount=4000.00,
            payee_name="Wire Transfer Services LLC",
            bank_name="PNC BANK",
            customer_name="David Mitchell",
            amount_mismatch=True,
            amount_words="Two Thousand",
            memo="Transfer",
            check_number="3300",
        ),
    },
]


def main():
    conn_str   = os.environ["SERVICE_BUS_CONNECTION_STRING"]
    queue_name = os.environ["TIER1_QUEUE_NAME"]

    print(f"Connecting to Service Bus queue: {queue_name}")

    with ServiceBusClient.from_connection_string(conn_str) as client:
        with client.get_queue_sender(queue_name) as sender:
            for scenario in SCENARIOS:
                payload = scenario["payload"]
                msg     = ServiceBusMessage(json.dumps(payload))
                sender.send_messages(msg)
                print(
                    f"  Sent: {scenario['name']} | "
                    f"check_id={payload['id']} | "
                    f"amount=${payload['amount']:,.2f}"
                )

    print(f"\nSent {len(SCENARIOS)} test payloads to {queue_name}")
    print("Wait ~30-60 seconds, then check the dashboard:")
    print("  curl -s https://checkfraudagent.azurewebsites.net/api/checks/queue")


if __name__ == "__main__":
    main()