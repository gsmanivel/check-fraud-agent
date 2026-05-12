import os, logging
from datetime import datetime, timezone
from azure.cosmos import CosmosClient

logger = logging.getLogger(__name__)


def get_cosmos_container(container_name: str):
    client = CosmosClient(url=os.environ["COSMOS_ENDPOINT"], credential=os.environ["COSMOS_KEY"])
    db = client.get_database_client(os.environ["COSMOS_DATABASE"])
    return db.get_container_client(container_name)


def get_customer(account_number: str):
    container = get_cosmos_container(os.environ["COSMOS_CUSTOMERS_CONTAINER"])
    query = f"SELECT * FROM c WHERE c.account_number = '{account_number}'"
    items = list(container.query_items(query=query, enable_cross_partition_query=True))
    return items[0] if items else None


def upsert_check(check: dict):
    check["updated_at"] = datetime.now(timezone.utc).isoformat()
    container = get_cosmos_container(os.environ["COSMOS_CHECKS_CONTAINER"])
    container.upsert_item(check)


def write_audit_log(check_id: str, tier: str, decision: str, details: dict):
    container = get_cosmos_container(os.environ["COSMOS_AUDIT_CONTAINER"])
    log_entry = {
        "id": f"{check_id}-{tier}-{int(datetime.now(timezone.utc).timestamp())}",
        "check_id": check_id, "tier": tier, "decision": decision,
        "details": details, "timestamp": datetime.now(timezone.utc).isoformat()
    }
    container.create_item(log_entry)
