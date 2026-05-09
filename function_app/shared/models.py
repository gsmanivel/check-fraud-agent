import os
import json
import logging
from datetime import datetime, timezone
from enum import Enum
from dataclasses import dataclass, field, asdict
from typing import Optional
from azure.cosmos import CosmosClient
from azure.servicebus import ServiceBusClient, ServiceBusMessage
from azure.keyvault.secrets import SecretClient
from azure.identity import DefaultAzureCredential

logger = logging.getLogger(__name__)


# ── Enums ────────────────────────────────────────────────────────────────────

class FraudDecision(str, Enum):
    APPROVE  = "approve"
    REJECT   = "reject"
    ESCALATE = "escalate"

class FraudPattern(str, Enum):
    STRUCTURING        = "structuring"
    ALTERED_CHECK      = "altered_check"
    SYNTHETIC_IDENTITY = "synthetic_identity"
    UNKNOWN            = "unknown"

class ProcessingTier(str, Enum):
    TIER1 = "tier1"
    TIER2 = "tier2"
    TIER3 = "tier3"


# ── Data models ───────────────────────────────────────────────────────────────

@dataclass
class ExtractedCheckFields:
    amount_numeric: float
    amount_words: str
    payee_name: str
    payee_match: bool
    micr_valid: bool
    signature_present: bool
    amount_mismatch: bool
    mismatch_type: Optional[str] = None
    alteration_detected: bool = False
    raw_ocr_confidence: float = 1.0


@dataclass
class CheckPayload:
    id: str
    check_number: str
    micr_line: str
    account_number: str
    routing_number: str
    customer_name: str
    payee_name: str
    amount: float
    memo: str
    issue_date: str
    submission_date: str
    bank_name: str
    check_image_url: str
    blob_path: str
    extracted_fields: ExtractedCheckFields
    status: str = "pending"
    processing_tier: Optional[str] = None
    fraud_decision: Optional[str] = None
    fraud_pattern: Optional[str] = None
    risk_score: int = 0
    fraud_indicators: list = field(default_factory=list)
    agent_reasoning: Optional[str] = None
    analyst_decision: Optional[str] = None
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self):
        d = asdict(self)
        return d

    @classmethod
    def from_dict(cls, d: dict):
        ef = d.pop("extracted_fields", {})
        d["extracted_fields"] = ExtractedCheckFields(**ef)
        return cls(**d)


@dataclass
class Tier1Result:
    check_id: str
    decision: FraudDecision
    risk_score: int
    fraud_indicators: list
    checks_run: dict
    processing_time_ms: int
    tier: str = ProcessingTier.TIER1


@dataclass
class Tier2Result:
    check_id: str
    decision: FraudDecision
    risk_score: int
    fraud_pattern: FraudPattern
    fraud_indicators: list
    agent_reasoning: str
    tool_calls_made: list
    iterations: int
    processing_time_ms: int
    tier: str = ProcessingTier.TIER2


# ── Cosmos DB client ──────────────────────────────────────────────────────────

def get_cosmos_container(container_name: str):
    client = CosmosClient(
        url=os.environ["COSMOS_ENDPOINT"],
        credential=os.environ["COSMOS_KEY"]
    )
    db = client.get_database_client(os.environ["COSMOS_DATABASE"])
    return db.get_container_client(container_name)


def get_check(check_id: str) -> dict:
    container = get_cosmos_container(os.environ["COSMOS_CHECKS_CONTAINER"])
    return container.read_item(item=check_id, partition_key=check_id)


def upsert_check(check: dict):
    check["updated_at"] = datetime.now(timezone.utc).isoformat()
    container = get_cosmos_container(os.environ["COSMOS_CHECKS_CONTAINER"])
    container.upsert_item(check)


def get_customer(account_number: str) -> Optional[dict]:
    container = get_cosmos_container(os.environ["COSMOS_CUSTOMERS_CONTAINER"])
    query = f"SELECT * FROM c WHERE c.account_number = '{account_number}'"
    items = list(container.query_items(query=query, enable_cross_partition_query=True))
    return items[0] if items else None


def write_audit_log(check_id: str, tier: str, decision: str, details: dict):
    container = get_cosmos_container(os.environ["COSMOS_AUDIT_CONTAINER"])
    log_entry = {
        "id": f"{check_id}-{tier}-{datetime.now(timezone.utc).timestamp()}",
        "check_id": check_id,
        "tier": tier,
        "decision": decision,
        "details": details,
        "timestamp": datetime.now(timezone.utc).isoformat()
    }
    container.create_item(log_entry)


# ── Service Bus helpers ───────────────────────────────────────────────────────

def enqueue_message(queue_name: str, payload: dict):
    conn_str = os.environ["SERVICE_BUS_CONNECTION_STRING"]
    with ServiceBusClient.from_connection_string(conn_str) as client:
        with client.get_queue_sender(queue_name) as sender:
            msg = ServiceBusMessage(json.dumps(payload))
            sender.send_messages(msg)
    logger.info(f"Enqueued message to {queue_name} for check {payload.get('id')}")
