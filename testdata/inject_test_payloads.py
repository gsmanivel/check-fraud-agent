"""
inject_and_cleanup.py — cleans up stale data then injects test payloads.

Scenarios are loaded from testData/scenarios.json — edit that file
to change account numbers, amounts, fraud signals etc.

Run before every demo:
    python testData/inject_and_cleanup.py
"""
import json
import os
import sys
import uuid
from datetime import datetime, timezone

ENGINE = 1

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
from azure.cosmos import CosmosClient

cosmos = CosmosClient(
    url=os.environ["COSMOS_ENDPOINT"],
    credential=os.environ["COSMOS_KEY"],
)
db = cosmos.get_database_client(os.environ["COSMOS_DATABASE"])


ENGINE_MAP = {
    1: "native",
    2: "sk",
    3: "azure_agent",
}
TIER2_ENGINE = ENGINE_MAP[ENGINE]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def build_payload(scenario: dict) -> dict:
    check_id = str(uuid.uuid4())
    return {
        "id":              check_id,
        "check_number":    scenario["check_number"],
        "account_number":  scenario["account_number"],
        "routing_number":  scenario["routing_number"],
        "customer_name":   scenario["customer_name"],
        "payee_name":      scenario["payee_name"],
        "amount":          scenario["amount"],
        "memo":            scenario.get("memo", ""),
        "issue_date":      now_iso(),
        "submission_date": now_iso(),
        "bank_name":       scenario["bank_name"],
        "check_image_url": f"https://test/{check_id}.jpg",
        "blob_path":       f"test/{check_id}.jpg",
        "tier2_engine": TIER2_ENGINE,
        "extracted_fields": {
            "amount_numeric":      scenario["amount"],
            "amount_words":        scenario.get("amount_words", ""),
            "payee_name":          scenario["payee_name"],
            "payee_match":         True,
            "micr_valid":          scenario.get("micr_valid", True),
            "signature_present":   scenario.get("signature_present", True),
            "amount_mismatch":     scenario.get("amount_mismatch", False),
            "alteration_detected": scenario.get("alteration_detected", False),
            "raw_ocr_confidence":  scenario.get("raw_ocr_confidence", 0.95),
            "account_number":      scenario["account_number"],
            "routing_number":      scenario["routing_number"],
            "bank_name":           scenario["bank_name"],
            "memo":                scenario.get("memo", ""),
        },
        "status":     "pending",
        "created_at": now_iso(),
        "updated_at": now_iso(),
    }


# ── load scenarios ─────────────────────────────────────────────────────────

def load_scenarios() -> list:
    scenarios_path = os.path.join(os.path.dirname(__file__), "scenarios.json")
    if not os.path.exists(scenarios_path):
        print(f"ERROR: scenarios.json not found at {scenarios_path}")
        sys.exit(1)
    with open(scenarios_path) as f:
        return json.load(f)


# ── Step 1: Clean Cosmos containers ────────────────────────────────────────

def cleanup_cosmos():
    print("\n[1/3] Cleaning Cosmos DB...")
    for container_name, pk in [
        (os.environ["COSMOS_CHECKS_CONTAINER"], "/id"),
        (os.environ["COSMOS_AUDIT_CONTAINER"],  "/check_id"),
    ]:
        try:
            db.delete_container(container_name)
            print(f"      Deleted:   {container_name}")
        except Exception:
            pass
        db.create_container(
            id=container_name,
            partition_key={"paths": [pk], "kind": "Hash"},
        )
        print(f"      Recreated: {container_name}")
    print("      Done.")


# ── Step 2: Drain Service Bus queues ───────────────────────────────────────

def drain_queues():
    print("\n[2/3] Draining Service Bus queues...")
    conn_str = os.environ["SERVICE_BUS_CONNECTION_STRING"]
    queues   = [
        os.environ["TIER1_QUEUE_NAME"],
        os.environ["TIER2_QUEUE_NAME"],
        os.environ["TIER3_QUEUE_NAME"],
    ]
    with ServiceBusClient.from_connection_string(conn_str) as client:
        for queue_name in queues:
            drained = 0
            try:
                with client.get_queue_receiver(
                    queue_name, max_wait_time=3,
                ) as receiver:
                    msgs = receiver.receive_messages(
                        max_message_count=100, max_wait_time=3,
                    )
                    for msg in msgs:
                        receiver.complete_message(msg)
                        drained += 1
            except Exception as e:
                print(f"      Warning draining {queue_name}: {e}")
            print(f"      {queue_name}: drained {drained} messages")
    print("      Done.")


# ── Step 3: Inject test payloads ───────────────────────────────────────────

def inject_payloads(scenarios: list):
    print(f"\n[3/3] Injecting {len(scenarios)} test payloads to Tier 1 queue...")
    conn_str   = os.environ["SERVICE_BUS_CONNECTION_STRING"]
    queue_name = os.environ["TIER1_QUEUE_NAME"]
    injected   = []

    with ServiceBusClient.from_connection_string(conn_str) as client:
        with client.get_queue_sender(queue_name) as sender:
            for scenario in scenarios:
                payload = build_payload(scenario)
                msg     = ServiceBusMessage(json.dumps(payload))
                sender.send_messages(msg)
                injected.append({
                    "scenario": scenario["name"],
                    "expected": scenario["expected"],
                    "check_id": payload["id"],
                    "amount":   payload["amount"],
                    "account":  payload["account_number"],
                })
                print(
                    f"      ✅ {scenario['name']:<35} "
                    f"account={payload['account_number']}  "
                    f"check_id={payload['id'][:8]}..."
                )

    manifest_path = os.path.join(os.path.dirname(__file__), ".demo_manifest.json")
    with open(manifest_path, "w") as f:
        json.dump(injected, f, indent=2)
    print(f"\n      Manifest saved → {manifest_path}")


# ── main ───────────────────────────────────────────────────────────────────

def main():
    print("=" * 65)
    print("  Check Fraud Demo — Cleanup + Inject")
    print("=" * 65)

    scenarios = load_scenarios()
    print(f"\n  Loaded {len(scenarios)} scenarios from scenarios.json")

    cleanup_cosmos()
    drain_queues()
    inject_payloads(scenarios)

    print("\n" + "=" * 65)
    print("  Done. Pipeline is running.")
    print("  Wait ~60s, then: python testData/check_status.py")
    print("  Dashboard: https://checkfraudagent.azurewebsites.net/api/dashboard")
    print("=" * 65)


if __name__ == "__main__":
    main()