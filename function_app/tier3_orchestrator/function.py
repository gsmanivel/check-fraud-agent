import os
import json
import logging
import azure.functions as func
import azure.durable_functions as df
from datetime import datetime, timezone, timedelta
import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from shared.models import upsert_check, write_audit_log

logger = logging.getLogger(__name__)

app = df.DFApp(http_auth_level=func.AuthLevel.FUNCTION)


# ── 1. Service Bus Trigger → starts the Durable orchestration ─────────────────

@app.service_bus_queue_trigger(
    arg_name="msg",
    queue_name="%TIER3_QUEUE_NAME%",
    connection="SERVICE_BUS_CONNECTION_STRING"
)
@app.durable_client_input(client_name="client")
async def tier3_starter(msg: func.ServiceBusMessage, client):
    """
    Triggered by Service Bus when Tier 2 escalates a check.
    Starts a Durable orchestration instance per check.
    """
    payload = json.loads(msg.get_body().decode("utf-8"))
    check_id = payload.get("id")
    logger.info(f"Tier 3 starting orchestration for check: {check_id}")

    instance_id = await client.start_new(
        orchestration_function_name="tier3_orchestrator",
        instance_id=check_id,
        client_input=payload
    )
    logger.info(f"Orchestration started: instance_id={instance_id}")
    msg.complete()


# ── 2. Orchestrator — waits for human event ───────────────────────────────────

@app.orchestration_trigger(context_name="context")
def tier3_orchestrator(context: df.DurableOrchestrationContext):
    """
    Durable orchestration that:
    1. Assigns check to analyst queue
    2. Waits up to 24 hours for analyst decision
    3. Records decision and closes
    """
    payload = context.get_input()
    check_id = payload.get("id")

    # Notify analyst queue
    yield context.call_activity("notify_analyst", payload)

    # Wait for analyst decision — up to 24 hours
    deadline = context.current_utc_datetime + timedelta(hours=24)

    analyst_event = context.wait_for_external_event("analyst_decision")
    timeout_event = context.create_timer(deadline)

    winner = yield context.task_any([analyst_event, timeout_event])

    if winner == analyst_event:
        decision_data = analyst_event.result
        yield context.call_activity("record_analyst_decision", {
            "payload": payload,
            "decision_data": decision_data
        })
        timeout_event.cancel()
        logger.info(f"Analyst decision received for check: {check_id}")
    else:
        # Timeout — auto-flag for senior review
        yield context.call_activity("record_analyst_decision", {
            "payload": payload,
            "decision_data": {
                "decision": "timeout",
                "analyst_id": "system",
                "notes": "No analyst decision received within 24 hours. Flagged for senior review."
            }
        })
        logger.warning(f"Analyst timeout for check: {check_id}")


# ── 3. Activity: notify analyst ───────────────────────────────────────────────

@app.activity_trigger(input_name="payload")
def notify_analyst(payload: dict):
    """
    Marks the check as awaiting analyst review in Cosmos DB.
    In production: sends email/Teams notification to analyst team.
    """
    check_id = payload.get("id")
    payload["status"] = "awaiting_analyst"
    payload["tier3_assigned_at"] = datetime.now(timezone.utc).isoformat()
    upsert_check(payload)

    write_audit_log(check_id, "tier3", "awaiting_analyst", {
        "tier2_reasoning": payload.get("agent_reasoning"),
        "risk_score": payload.get("risk_score"),
        "fraud_pattern": payload.get("fraud_pattern"),
        "fraud_indicators": payload.get("fraud_indicators", [])
    })

    logger.info(f"Check {check_id} assigned to analyst queue")
    return {"status": "notified", "check_id": check_id}


# ── 4. Activity: record analyst decision ──────────────────────────────────────

@app.activity_trigger(input_name="data")
def record_analyst_decision(data: dict):
    """
    Records the analyst's final decision to Cosmos DB and audit log.
    Updates the check status and triggers feedback loop.
    """
    payload = data["payload"]
    decision_data = data["decision_data"]
    check_id = payload.get("id")

    analyst_decision = decision_data.get("decision")
    analyst_id = decision_data.get("analyst_id", "unknown")
    notes = decision_data.get("notes", "")
    override_pattern = decision_data.get("fraud_pattern")

    payload["analyst_decision"] = analyst_decision
    payload["analyst_id"] = analyst_id
    payload["analyst_notes"] = notes
    payload["status"] = f"analyst_{analyst_decision}"
    payload["tier3_completed_at"] = datetime.now(timezone.utc).isoformat()

    if override_pattern:
        payload["fraud_pattern"] = override_pattern

    upsert_check(payload)

    write_audit_log(check_id, "tier3", f"analyst_{analyst_decision}", {
        "analyst_id": analyst_id,
        "analyst_decision": analyst_decision,
        "analyst_notes": notes,
        "fraud_pattern_confirmed": override_pattern or payload.get("fraud_pattern")
    })

    logger.info(f"Analyst decision recorded: {check_id} → {analyst_decision} by {analyst_id}")
    return {"status": "recorded", "check_id": check_id, "decision": analyst_decision}


# ── 5. HTTP endpoint — analyst submits decision ───────────────────────────────

@app.route(route="checks/{check_id}/decision", methods=["POST"])
@app.durable_client_input(client_name="client")
async def analyst_decision_endpoint(req: func.HttpRequest, client) -> func.HttpResponse:
    """
    Called by the analyst dashboard when analyst clicks Approve/Reject.
    Raises the external event that the orchestrator is waiting for.

    POST /api/checks/{check_id}/decision
    Body: {
      "decision": "approve" | "reject",
      "analyst_id": "analyst@company.com",
      "notes": "Optional notes",
      "fraud_pattern": "structuring" (optional override)
    }
    """
    check_id = req.route_params.get("check_id")
    try:
        body = req.get_json()
    except Exception:
        return func.HttpResponse("Invalid JSON body", status_code=400)

    decision = body.get("decision")
    if decision not in ["approve", "reject"]:
        return func.HttpResponse("decision must be 'approve' or 'reject'", status_code=400)

    # Raise the external event — orchestrator wakes up
    await client.raise_event(
        instance_id=check_id,
        event_name="analyst_decision",
        event_data=body
    )

    logger.info(f"Analyst decision raised for check {check_id}: {decision}")
    return func.HttpResponse(
        json.dumps({"status": "decision_recorded", "check_id": check_id, "decision": decision}),
        status_code=200,
        mimetype="application/json"
    )


# ── 6. HTTP endpoint — get check status ──────────────────────────────────────

@app.route(route="checks/{check_id}/status", methods=["GET"])
@app.durable_client_input(client_name="client")
async def get_check_status(req: func.HttpRequest, client) -> func.HttpResponse:
    """
    Returns the current orchestration status for a check.
    Used by the analyst dashboard to poll for updates.
    """
    check_id = req.route_params.get("check_id")
    status = await client.get_status(check_id)

    if not status:
        return func.HttpResponse(
            json.dumps({"error": "Check not found"}),
            status_code=404,
            mimetype="application/json"
        )

    return func.HttpResponse(
        json.dumps({
            "check_id": check_id,
            "orchestration_status": status.runtime_status.value,
            "created_time": str(status.created_time),
            "last_updated_time": str(status.last_updated_time)
        }),
        status_code=200,
        mimetype="application/json"
    )


# ── 7. HTTP endpoint — analyst review queue ───────────────────────────────────

@app.route(route="checks/queue", methods=["GET"])
def get_analyst_queue(req: func.HttpRequest) -> func.HttpResponse:
    """
    Returns all checks currently awaiting analyst review.
    Used by the analyst dashboard to show the review queue.
    """
    from shared.models import get_cosmos_container
    container = get_cosmos_container(os.environ["COSMOS_CHECKS_CONTAINER"])

    query = """
        SELECT c.id, c.check_number, c.amount, c.payee_name,
               c.account_number, c.risk_score, c.fraud_pattern,
               c.fraud_indicators, c.agent_reasoning,
               c.check_image_url, c.tier3_assigned_at
        FROM c
        WHERE c.status = 'awaiting_analyst'
        ORDER BY c.tier3_assigned_at ASC
    """
    items = list(container.query_items(query=query, enable_cross_partition_query=True))

    return func.HttpResponse(
        json.dumps({"queue": items, "count": len(items)}),
        status_code=200,
        mimetype="application/json"
    )
