"""
Test scenario runner for the check-fraud-agent system.

Usage:
  python scripts/run_scenarios.py --scenario A
  python scripts/run_scenarios.py --scenario C --engine sk
  python scripts/run_scenarios.py --scenario G --engine native --poll
  python scripts/run_scenarios.py --all
  python scripts/run_scenarios.py --list
"""

import argparse, json, os, sys, time, uuid
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
settings = json.load(open("local.settings.json"))["Values"]
for k, v in settings.items():
    os.environ.setdefault(k, v)

from azure.servicebus import ServiceBusClient, ServiceBusMessage

BASE_URL    = "https://checkfraudagent.azurewebsites.net"
TIER1_QUEUE = settings["TIER1_QUEUE_NAME"]
SB_CONN     = settings["SERVICE_BUS_CONNECTION_STRING"]

# ---------------------------------------------------------------------------
# Scenario definitions
# ---------------------------------------------------------------------------

def _base(account_number, customer_name, payee_name, amount, bank_name, ef_overrides=None):
    now = datetime.now(timezone.utc).isoformat()
    ef  = {
        "micr_valid":          True,
        "amount_mismatch":     False,
        "payee_match":         True,
        "alteration_detected": False,
        "signature_present":   True,
        "raw_ocr_confidence":  0.98,
    }
    if ef_overrides:
        ef.update(ef_overrides)
    return {
        "id":              str(uuid.uuid4()),
        "check_number":    f"CHK-{str(uuid.uuid4())[:8].upper()}",
        "account_number":  account_number,
        "routing_number":  "122105155",
        "customer_name":   customer_name,
        "payee_name":      payee_name,
        "amount":          amount,
        "memo":            "See scenario documentation",
        "issue_date":      now,
        "submission_date": now,
        "bank_name":       bank_name,
        "blob_path":       f"check-images/scenario-test.jpg",
        "check_image_url": f"https://checkfraudstorage.blob.core.windows.net/check-images/scenario-test.jpg",
        "status":          "pending",
        "created_at":      now,
        "updated_at":      now,
        "extracted_fields": ef,
    }


SCENARIOS = {
    "A": {
        "name":     "Clean Legitimate Check",
        "expected": "Tier-1 APPROVE (risk_score: 0)",
        "path":     "Tier-1 only",
        "payload":  _base(
            account_number = "4645120421",
            customer_name  = "Steven Wilson",
            payee_name     = "Office Depot",
            amount         = 1500.00,
            bank_name      = "Capital One",
        ),
    },
    "B": {
        "name":     "Document Fraud — Altered Check",
        "expected": "Tier-1 REJECT (risk_score: 100)",
        "path":     "Tier-1 only",
        "payload":  _base(
            account_number = "1478163327",
            customer_name  = "David Mitchell",
            payee_name     = "Unknown Payee",
            amount         = 7500.00,
            bank_name      = "Chase",
            ef_overrides   = {
                "micr_valid":         False,
                "amount_mismatch":    True,
                "signature_present":  False,
            },
        ),
    },
    "C": {
        "name":     "Structuring — New Account Near CTR",
        "expected": "Tier-1 ESCALATE (risk_score: 45) → Tier-2 investigates structuring",
        "path":     "Tier-1 → Tier-2",
        "payload":  _base(
            account_number = "7857221324",
            customer_name  = "Ashley Hill",
            payee_name     = "Global Ventures LLC",
            amount         = 9200.00,
            bank_name      = "Wells Fargo",
        ),
    },
    "D": {
        "name":     "Synthetic Identity Detection",
        "expected": "Tier-1 ESCALATE (risk_score: 70) → Tier-2 flags synthetic identity",
        "path":     "Tier-1 → Tier-2",
        "payload":  _base(
            account_number = "9190197115",
            customer_name  = "Patricia Adams",
            payee_name     = "Sunrise Consulting",
            amount         = 3500.00,
            bank_name      = "Bank of America",
        ),
    },
    "E": {
        "name":     "New Account + Oversized Check",
        "expected": "Tier-1 ESCALATE (risk_score: 40) → Tier-2 investigates",
        "path":     "Tier-1 → Tier-2",
        "payload":  _base(
            account_number = "7803990970",
            customer_name  = "Linda Allen",
            payee_name     = "Apex Holdings",
            amount         = 12000.00,
            bank_name      = "US Bank",
        ),
    },
    "F": {
        "name":     "Full Chain — Reaches Human Analyst",
        "expected": "Tier-1 ESCALATE (risk_score: 70) → Tier-2 escalates → Tier-3 analyst queue",
        "path":     "Tier-1 → Tier-2 → Tier-3",
        "payload":  _base(
            account_number = "6237376063",
            customer_name  = "Anthony Perez",
            payee_name     = "Summit Capital Partners",
            amount         = 2500.00,
            bank_name      = "Chase",
        ),
    },
    "G": {
        "name":     "Engine Comparison — Same Check, Both Engines",
        "expected": "Tier-1 ESCALATE (risk_score: 40) → Tier-2 (run twice: native then sk)",
        "path":     "Tier-1 → Tier-2",
        "payload":  _base(
            account_number = "1478163327",
            customer_name  = "David Mitchell",
            payee_name     = "Harbor Services Inc",
            amount         = 8700.00,
            bank_name      = "Chase",
            ef_overrides   = {
                "signature_present": False,
            },
        ),
    },
    "X1": {
        "name":     "Edge Case — Account Not Found",
        "expected": "Tier-1 ESCALATE (risk_score: 60, account_not_found) → Tier-2 unknown account",
        "path":     "Tier-1 → Tier-2",
        "payload":  _base(
            account_number = "9999999999",
            customer_name  = "Unknown Person",
            payee_name     = "Test Payee",
            amount         = 500.00,
            bank_name      = "Test Bank",
        ),
    },
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def print_separator(char="─", width=65):
    print(char * width)


def send_to_tier1(payload: dict) -> str:
    with ServiceBusClient.from_connection_string(SB_CONN) as client:
        with client.get_queue_sender(TIER1_QUEUE) as sender:
            sender.send_messages(ServiceBusMessage(json.dumps(payload)))
    return payload["id"]


def poll_status(check_id: str, max_wait: int = 60) -> dict | None:
    try:
        import urllib.request
        url = f"{BASE_URL}/api/checks/{check_id}/status"
        deadline = time.time() + max_wait
        print(f"\n  Polling status (up to {max_wait}s)...", end="", flush=True)
        while time.time() < deadline:
            time.sleep(4)
            print(".", end="", flush=True)
            try:
                with urllib.request.urlopen(url, timeout=5) as r:
                    data = json.loads(r.read())
                    if data.get("status") not in (None, "pending"):
                        print()
                        return data
            except Exception:
                pass
        print("\n  (timed out waiting for result)")
        return None
    except Exception as e:
        print(f"\n  Could not poll status: {e}")
        return None


def override_engine(engine: str):
    """Patch TIER2_ENGINE in local.settings.json for the current run."""
    if not engine:
        return
    path = "local.settings.json"
    cfg  = json.load(open(path))
    cfg["Values"]["TIER2_ENGINE"] = engine
    with open(path, "w") as f:
        json.dump(cfg, f, indent=2)
    print(f"  [config] TIER2_ENGINE set to: {engine}")


def run_scenario(key: str, engine: str = None, poll: bool = False):
    if key not in SCENARIOS:
        print(f"Unknown scenario: {key}. Use --list to see available scenarios.")
        return

    s = SCENARIOS[key]
    payload = dict(s["payload"])
    payload["id"] = str(uuid.uuid4())  # fresh ID each run

    print_separator("═")
    print(f"  SCENARIO {key} — {s['name']}")
    print_separator()
    print(f"  Account  : {payload['account_number']} ({payload['customer_name']})")
    print(f"  Amount   : ${payload['amount']:,.2f}")
    print(f"  Payee    : {payload['payee_name']}")
    print(f"  Bank     : {payload['bank_name']}")
    ef = payload['extracted_fields']
    print(f"  MICR OK  : {ef['micr_valid']}   Mismatch: {ef['amount_mismatch']}   Sig: {ef['signature_present']}   OCR: {ef['raw_ocr_confidence']:.0%}")
    print_separator()
    print(f"  Expected : {s['expected']}")
    print(f"  Path     : {s['path']}")
    if engine:
        print(f"  Engine   : {engine} (overriding TIER2_ENGINE)")
        override_engine(engine)
    print_separator()

    check_id = send_to_tier1(payload)
    print(f"  Sent!    check_id = {check_id}")
    print()
    print(f"  Verify:")
    print(f"    Cosmos  : checks container → id = {check_id}")
    print(f"    Status  : GET {BASE_URL}/api/checks/{check_id}/status")
    print(f"    Queue   : GET {BASE_URL}/api/checks/queue  (if escalated to Tier-3)")
    print(f"    Dash    : {BASE_URL}/api/dashboard")

    if poll:
        result = poll_status(check_id)
        if result:
            print()
            print_separator()
            print(f"  RESULT:")
            print(f"    status        : {result.get('status')}")
            print(f"    fraud_decision: {result.get('fraud_decision')}")
            print(f"    analyst_dec   : {result.get('analyst_decision', '—')}")
            print_separator()

    print()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Check Fraud Agent — Scenario Runner")
    parser.add_argument("--scenario", metavar="KEY", help="Scenario to run (A/B/C/D/E/F/G/X1)")
    parser.add_argument("--all",      action="store_true", help="Run all main scenarios (A–G)")
    parser.add_argument("--list",     action="store_true", help="List all available scenarios")
    parser.add_argument("--engine",   choices=["native", "sk"], help="Override TIER2_ENGINE")
    parser.add_argument("--poll",     action="store_true", help="Poll status API after sending")
    args = parser.parse_args()

    if args.list:
        print_separator("═")
        print("  AVAILABLE SCENARIOS")
        print_separator()
        for key, s in SCENARIOS.items():
            print(f"  {key:4}  {s['name']}")
            print(f"        Expected: {s['expected']}")
            print()
        return

    if args.all:
        for key in ["A", "B", "C", "D", "E", "F", "G"]:
            run_scenario(key, engine=args.engine, poll=args.poll)
            time.sleep(2)
        return

    if args.scenario:
        run_scenario(args.scenario.upper(), engine=args.engine, poll=args.poll)
        return

    parser.print_help()


if __name__ == "__main__":
    main()
