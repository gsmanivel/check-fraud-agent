import os, json, logging
import azure.functions as func
import azure.durable_functions as df
from datetime import datetime, timezone, timedelta

from handlers.shared.cosmos import get_cosmos_container, upsert_check, write_audit_log

logger = logging.getLogger(__name__)
bp = df.Blueprint()


@bp.service_bus_queue_trigger(
    arg_name="msg",
    queue_name="%TIER3_QUEUE_NAME%",
    connection="SERVICE_BUS_CONNECTION_STRING"
)
@bp.durable_client_input(client_name="client")
async def tier3_starter(msg: func.ServiceBusMessage, client):
    payload     = json.loads(msg.get_body().decode("utf-8"))
    check_id    = payload.get("id")
    instance_id = await client.start_new("tier3_orchestrator", instance_id=check_id, client_input=payload)
    logger.info(f"Tier 3 orchestration started: {instance_id}")
    msg.complete()


@bp.orchestration_trigger(context_name="context")
def tier3_orchestrator(context: df.DurableOrchestrationContext):
    payload = context.get_input()
    yield context.call_activity("notify_analyst", payload)
    deadline      = context.current_utc_datetime + timedelta(hours=24)
    analyst_event = context.wait_for_external_event("analyst_decision")
    timeout_event = context.create_timer(deadline)
    winner        = yield context.task_any([analyst_event, timeout_event])
    if winner == analyst_event:
        yield context.call_activity("record_analyst_decision", {"payload": payload, "decision_data": analyst_event.result})
        timeout_event.cancel()
    else:
        yield context.call_activity("record_analyst_decision", {
            "payload": payload,
            "decision_data": {"decision": "timeout", "analyst_id": "system", "notes": "No response in 24h"}
        })


@bp.activity_trigger(input_name="payload")
def notify_analyst(payload: dict):
    check_id = payload.get("id")
    payload["status"]            = "awaiting_analyst"
    payload["tier3_assigned_at"] = datetime.now(timezone.utc).isoformat()
    upsert_check(payload)
    write_audit_log(check_id, "tier3", "awaiting_analyst", {
        "risk_score": payload.get("risk_score"), "fraud_pattern": payload.get("fraud_pattern")
    })
    return {"status": "notified", "check_id": check_id}


@bp.activity_trigger(input_name="activityInput")
def record_analyst_decision(activityInput: dict):
    payload       = activityInput["payload"]
    decision_data = activityInput["decision_data"]
    check_id      = payload.get("id")
    payload.update({
        "analyst_decision":   decision_data.get("decision"),
        "analyst_id":         decision_data.get("analyst_id", "unknown"),
        "analyst_notes":      decision_data.get("notes", ""),
        "status":             f"analyst_{decision_data.get('decision')}",
        "tier3_completed_at": datetime.now(timezone.utc).isoformat()
    })
    upsert_check(payload)
    write_audit_log(check_id, "tier3", f"analyst_{decision_data.get('decision')}", decision_data)
    return {"status": "recorded", "check_id": check_id}


@bp.route(route="checks/{check_id}/decision", methods=["POST"])
@bp.durable_client_input(client_name="client")
async def analyst_decision_endpoint(req: func.HttpRequest, client) -> func.HttpResponse:
    check_id = req.route_params.get("check_id")
    try:
        body = req.get_json()
    except Exception:
        return func.HttpResponse("Invalid JSON", status_code=400)
    if body.get("decision") not in ["approve", "reject"]:
        return func.HttpResponse("decision must be approve or reject", status_code=400)
    await client.raise_event(instance_id=check_id, event_name="analyst_decision", event_data=body)
    return func.HttpResponse(
        json.dumps({"status": "recorded", "check_id": check_id, "decision": body.get("decision")}),
        status_code=200, mimetype="application/json"
    )


@bp.route(route="checks/{check_id}/status", methods=["GET"])
@bp.durable_client_input(client_name="client")
async def get_check_status(req: func.HttpRequest, client) -> func.HttpResponse:
    check_id = req.route_params.get("check_id")
    status   = await client.get_status(check_id)
    if not status:
        return func.HttpResponse(json.dumps({"error": "Not found"}), status_code=404, mimetype="application/json")
    return func.HttpResponse(
        json.dumps({"check_id": check_id, "status": status.runtime_status.value}),
        status_code=200, mimetype="application/json"
    )


@bp.route(route="checks/queue", methods=["GET"])
async def get_analyst_queue(req: func.HttpRequest) -> func.HttpResponse:
    container = get_cosmos_container(os.environ["COSMOS_CHECKS_CONTAINER"])
    query = "SELECT * FROM c WHERE c.status='awaiting_analyst' ORDER BY c.tier3_assigned_at ASC"
    items = list(container.query_items(query=query, enable_cross_partition_query=True))
    return func.HttpResponse(
        json.dumps({"queue": items, "count": len(items)}),
        status_code=200, mimetype="application/json"
    )


@bp.route(route="dashboard", methods=["GET"], auth_level=func.AuthLevel.ANONYMOUS)
async def serve_dashboard(req: func.HttpRequest) -> func.HttpResponse:
    here = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    html_path = os.path.join(here, "dashboard.html")
    with open(html_path, "r", encoding="utf-8") as f:
        html = f.read()
    return func.HttpResponse(html, status_code=200, mimetype="text/html")
