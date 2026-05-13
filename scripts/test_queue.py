import json, uuid, os
from datetime import datetime, timezone
from azure.servicebus import ServiceBusClient, ServiceBusMessage

settings = json.load(open("local.settings.json"))["Values"]
conn_str  = settings["SERVICE_BUS_CONNECTION_STRING"]
queue     = settings["TIER1_QUEUE_NAME"]

payload = {
    "id":             str(uuid.uuid4()),
    "check_number":   "1003",
    "account_number": "1478163327",
    "routing_number": "122105155",
    "customer_name":  "David Mitchell",
    "payee_name":     "Jane Doe",
    "amount":         7500.00,
    "memo":           "Invoice",
    "issue_date":     "2026-05-01",
    "submission_date": datetime.now(timezone.utc).isoformat(),
    "bank_name":      "Test Bank",
    "blob_path":      "check-images/test_1003.jpg",
    "check_image_url": "https://checkfraudstorage.blob.core.windows.net/check-images/test_1003.jpg",
    "status":         "pending",
    "created_at":     datetime.now(timezone.utc).isoformat(),
    "updated_at":     datetime.now(timezone.utc).isoformat(),
    "extracted_fields": {
        "micr_valid":           True,
        "amount_mismatch":      False,
        "payee_match":          True,
        "alteration_detected":  False,
        "signature_present":    False,
        "raw_ocr_confidence":   0.65
    }
}

with ServiceBusClient.from_connection_string(conn_str) as client:
    with client.get_queue_sender(queue) as sender:
        sender.send_messages(ServiceBusMessage(json.dumps(payload)))

print(f"✅ Sent check {payload['id']} to {queue}")
print(f"   Account : {payload['account_number']}")
print(f"   Amount  : ${payload['amount']:,.2f}")
print(f"   Payee   : {payload['payee_name']}")
