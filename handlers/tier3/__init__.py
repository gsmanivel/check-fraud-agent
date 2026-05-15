import os, json, logging
import azure.functions as func
import azure.durable_functions as df
from datetime import datetime, timezone

from handlers.shared.cosmos import get_cosmos_container, upsert_check, write_audit_log

logger = logging.getLogger(__name__)
bp = df.Blueprint()


@bp.service_bus_queue_trigger(
    arg_name="msg",
    queue_name="%TIER3_QUEUE_NAME%",
    connection="SERVICE_BUS_CONNECTION_STRING"
)
def tier3_intake(msg: func.ServiceBusMessage):
    payload  = json.loads(msg.get_body().decode("utf-8"))
    check_id = payload.get("id")
    payload["status"]            = "awaiting_analyst"
    payload["tier3_assigned_at"] = datetime.now(timezone.utc).isoformat()
    upsert_check(payload)
    write_audit_log(check_id, "tier3", "awaiting_analyst", {
        "risk_score": payload.get("risk_score"),
        "fraud_pattern": payload.get("fraud_pattern")
    })
    logger.info(f"Tier 3 intake: {check_id} awaiting analyst")


@bp.route(route="checks/{check_id}/decision", methods=["POST"], auth_level=func.AuthLevel.ANONYMOUS)
async def analyst_decision_endpoint(req: func.HttpRequest) -> func.HttpResponse:
    check_id = req.route_params.get("check_id")
    try:
        body = req.get_json()
    except Exception:
        return func.HttpResponse("Invalid JSON", status_code=400)
    decision = body.get("decision")
    if decision not in ["approve", "reject"]:
        return func.HttpResponse("decision must be approve or reject", status_code=400)

    container = get_cosmos_container(os.environ["COSMOS_CHECKS_CONTAINER"])
    rows = list(container.query_items(
        query="SELECT * FROM c WHERE c.id=@check_id",
        parameters=[{"name": "@check_id", "value": check_id}],
        enable_cross_partition_query=True
    ))
    if not rows:
        return func.HttpResponse(json.dumps({"error": "Check not found"}), status_code=404, mimetype="application/json")

    payload = rows[0]
    payload.update({
        "analyst_decision":   decision,
        "analyst_id":         body.get("analyst_id", "analyst@dashboard"),
        "analyst_notes":      body.get("notes", ""),
        "status":             f"analyst_{decision}",
        "tier3_completed_at": datetime.now(timezone.utc).isoformat()
    })
    upsert_check(payload)
    write_audit_log(check_id, "tier3", f"analyst_{decision}", body)
    logger.info(f"Analyst decision: {check_id} → {decision}")
    return func.HttpResponse(
        json.dumps({"status": "recorded", "check_id": check_id, "decision": decision}),
        status_code=200, mimetype="application/json"
    )


@bp.route(route="checks/{check_id}/status", methods=["GET"], auth_level=func.AuthLevel.ANONYMOUS)
async def get_check_status(req: func.HttpRequest) -> func.HttpResponse:
    check_id  = req.route_params.get("check_id")
    container = get_cosmos_container(os.environ["COSMOS_CHECKS_CONTAINER"])
    rows = list(container.query_items(
        query="SELECT c.id, c.status, c.fraud_decision, c.analyst_decision FROM c WHERE c.id=@check_id",
        parameters=[{"name": "@check_id", "value": check_id}],
        enable_cross_partition_query=True
    ))
    if not rows:
        return func.HttpResponse(json.dumps({"error": "Not found"}), status_code=404, mimetype="application/json")
    return func.HttpResponse(json.dumps(rows[0]), status_code=200, mimetype="application/json")


@bp.route(route="checks/queue", methods=["GET"], auth_level=func.AuthLevel.ANONYMOUS)
async def get_analyst_queue(req: func.HttpRequest) -> func.HttpResponse:
    container = get_cosmos_container(os.environ["COSMOS_CHECKS_CONTAINER"])
    query = (
        "SELECT * FROM c WHERE c.status IN ('awaiting_analyst', 'escalated_tier3') "
        "ORDER BY c.tier3_assigned_at ASC"
    )
    items = list(container.query_items(query=query, enable_cross_partition_query=True))
    return func.HttpResponse(
        json.dumps({"queue": items, "count": len(items)}),
        status_code=200, mimetype="application/json"
    )


@bp.route(route="dashboard", methods=["GET"], auth_level=func.AuthLevel.ANONYMOUS)
async def serve_dashboard(req: func.HttpRequest) -> func.HttpResponse:
    here      = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    html_path = os.path.join(here, "dashboard.html")
    with open(html_path, "r", encoding="utf-8") as f:
        html = f.read()
    return func.HttpResponse(html, status_code=200, mimetype="text/html")
