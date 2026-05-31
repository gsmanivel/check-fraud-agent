"""
check_status.py — prints a summary table of all 7 demo checks and their pipeline status.

Run 60 seconds after inject_and_cleanup.py:
    python testData/check_status.py
"""
import json
import os
import re
import sys
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

from azure.cosmos import CosmosClient

cosmos = CosmosClient(
    url=os.environ["COSMOS_ENDPOINT"],
    credential=os.environ["COSMOS_KEY"],
)
db = cosmos.get_database_client(os.environ["COSMOS_DATABASE"])

# ANSI colors
GREEN  = "\033[92m"
RED    = "\033[91m"
YELLOW = "\033[93m"
BLUE   = "\033[94m"
RESET  = "\033[0m"
BOLD   = "\033[1m"

STATUS_COLOR = {
    "approved":                   GREEN,
    "approved_by_analyst":        GREEN,
    "rejected":                   RED,
    "rejected_by_analyst":        RED,
    "fraud_confirmed_by_analyst": RED,
    "escalated_tier3":            YELLOW,
    "awaiting_analyst":           YELLOW,
    "escalating_to_tier2":        BLUE,
    "pending":                    BLUE,
}


_ANSI_RE = re.compile(r"\033\[[0-9;]*m")


def _visual_len(s: str) -> int:
    return len(_ANSI_RE.sub("", s))


def _ljust_ansi(s: str, width: int) -> str:
    return s + " " * max(0, width - _visual_len(s))


def color_status(status: str) -> str:
    c = STATUS_COLOR.get(status, RESET)
    return f"{c}{status}{RESET}"


def load_manifest() -> list:
    manifest_path = os.path.join(os.path.dirname(__file__), ".demo_manifest.json")
    if not os.path.exists(manifest_path):
        print("No manifest found. Run inject_and_cleanup.py first.")
        sys.exit(1)
    with open(manifest_path) as f:
        return json.load(f)


def get_check(check_id: str) -> dict:
    try:
        container = db.get_container_client(os.environ["COSMOS_CHECKS_CONTAINER"])
        return container.read_item(item=check_id, partition_key=check_id)
    except Exception:
        return {}


def get_audit(check_id: str) -> list:
    try:
        container = db.get_container_client(os.environ["COSMOS_AUDIT_CONTAINER"])
        query = (
            f"SELECT c.tier, c.decision, c.timestamp "
            f"FROM c WHERE c.check_id = '{check_id}' "
            f"ORDER BY c.timestamp ASC"
        )
        return list(container.query_items(
            query=query, enable_cross_partition_query=True
        ))
    except Exception:
        return []


def fmt_amount(amount) -> str:
    try:
        return f"${float(amount):,.2f}"
    except Exception:
        return str(amount)


def main():
    manifest = load_manifest()

    print("\n" + "=" * 125)
    print(f"  {BOLD}Check Fraud Demo — Pipeline Status{RESET}")
    print(f"  Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 125)
    print(
        f"\n  {'#':<3} {'Scenario':<40} {'Amount':<12} {'Expected':<12} "
        f"{'Status':<30} {'Tier':<8} {'Pattern'}"
    )
    print("  " + "-" * 119)

    correct = pending = wrong = 0

    for i, item in enumerate(manifest, 1):
        check    = get_check(item["check_id"])
        status   = check.get("status", "not_found") if check else "not_found"
        tier     = check.get("processing_tier", "-") if check else "-"
        pattern  = "-"
        reasoning = ""

        if check:
            # fraud_pattern may be top-level or in dict extra
            pattern   = (
                check.get("fraud_pattern")
                or check.get("agent_fraud_pattern")
                or "-"
            )
            reasoning = (
                check.get("agent_reasoning")
                or check.get("reasoning")
                or ""
            )

        expected         = item["expected"]
        decision_matches = (
            (expected == "approve"  and status in (
                "approve", "approved", "approved_by_analyst"
            )) or
            (expected == "reject"   and status in (
                "reject", "rejected", "rejected_by_analyst", "fraud_confirmed_by_analyst"
            )) or
            (expected == "escalate" and status in (
                "escalate", "escalated", "escalated_tier3",
                "awaiting_analyst", "escalating_to_tier2"
            ))
        )
        is_pending = status in ("pending", "not_found", "escalating_to_tier2")

        if is_pending:
            pending += 1
            marker = f"{BLUE}⏳{RESET}"
        elif decision_matches:
            correct += 1
            marker = f"{GREEN}✅{RESET}"
        else:
            wrong += 1
            marker = f"{RED}❌{RESET}"

        print(
            f"  {marker} {i:<2} {item['scenario']:<40} "
            f"{fmt_amount(item['amount']):<12} "
            f"{expected:<12} "
            f"{_ljust_ansi(color_status(status), 30)}"
            f"{tier:<8} "
            f"{pattern}"
        )

        # Agent reasoning (truncated to 120 chars)
        if reasoning and len(reasoning) > 10:
            short = reasoning[:120] + "..." if len(reasoning) > 120 else reasoning
            print(f"       {'':42}  {short}")

        # Audit trail
        audit = get_audit(item["check_id"])
        if audit:
            trail = " → ".join(
                f"{a.get('tier','?')}:{a.get('decision','?')}" for a in audit
            )
            print(f"       {'':42} {trail}")

        print()

    print("  " + "-" * 119)
    print(
        f"  {BOLD}Summary:{RESET}  "
        f"{GREEN}✅ {correct} correct{RESET}   "
        f"{BLUE}⏳ {pending} pending{RESET}   "
        f"{RED}❌ {wrong} wrong{RESET}   "
        f"Total: {len(manifest)}"
    )
    print()

    if pending:
        print(f"  {YELLOW}⚠  Some checks still processing. Wait 30s and run again.{RESET}")

    print(f"\n  Dashboard: https://checkfraudagent.azurewebsites.net/api/dashboard")
    print("=" * 125 + "\n")


if __name__ == "__main__":
    main()