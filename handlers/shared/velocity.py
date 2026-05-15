import os
from datetime import datetime, timezone, timedelta
from handlers.shared.cosmos import get_cosmos_container


def query_velocity(account_number: str, days_back: int = 1, exclude_check_id: str = "") -> dict:
    container = get_cosmos_container(os.environ["COSMOS_CHECKS_CONTAINER"])
    cutoff    = (datetime.now(timezone.utc) - timedelta(days=days_back)).isoformat()

    if exclude_check_id:
        base_where = "c.account_number=@acct AND c.submission_date>=@cutoff AND c.id!=@check_id"
        params     = [
            {"name": "@acct",     "value": account_number},
            {"name": "@cutoff",   "value": cutoff},
            {"name": "@check_id", "value": exclude_check_id},
        ]
    else:
        base_where = "c.account_number=@acct AND c.submission_date>=@cutoff"
        params     = [
            {"name": "@acct",   "value": account_number},
            {"name": "@cutoff", "value": cutoff},
        ]

    count   = (list(container.query_items(query=f"SELECT VALUE COUNT(1) FROM c WHERE {base_where}", parameters=params, enable_cross_partition_query=True)) or [0])[0]
    total   = (list(container.query_items(query=f"SELECT VALUE SUM(c.amount) FROM c WHERE {base_where}", parameters=params, enable_cross_partition_query=True)) or [0])[0] or 0
    txns    = list(container.query_items(query=f"SELECT c.amount, c.submission_date FROM c WHERE {base_where} ORDER BY c.submission_date DESC", parameters=params, enable_cross_partition_query=True))
    near_ctr = sum(1 for t in txns if 8500 <= t.get("amount", 0) <= 9999)

    return {"count": count, "total": round(total, 2), "near_ctr": near_ctr, "recent_txns": txns}
