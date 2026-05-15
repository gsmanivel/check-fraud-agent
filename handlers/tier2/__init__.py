import os, json, time, logging
import azure.functions as func
import azure.durable_functions as df

from handlers.shared.cosmos import upsert_check, write_audit_log
from handlers.shared.servicebus import enqueue_message

logger = logging.getLogger(__name__)
bp = df.Blueprint()


@bp.service_bus_queue_trigger(
    arg_name="msg",
    queue_name="%TIER2_QUEUE_NAME%",
    connection="SERVICE_BUS_CONNECTION_STRING"
)
async def tier2_agent(msg: func.ServiceBusMessage):
    start    = time.time()
    payload  = json.loads(msg.get_body().decode("utf-8"))
    check_id = payload.get("id")
    engine   = os.environ.get("TIER2_ENGINE", "native")
    logger.info(f"Tier 2 [{engine}] processing: {check_id}")

    try:
        if engine == "sk":
            from handlers.tier2.sk_agent import run_agent_sk
            result = await run_agent_sk(payload, start)
        else:
            from handlers.tier2.native import run_agent_native
            result = run_agent_native(payload, start)

        ms = int((time.time() - start) * 1000)
        payload.update({
            "risk_score":       result["risk_score"],
            "fraud_decision":   result["decision"],
            "fraud_pattern":    result.get("fraud_pattern"),
            "fraud_indicators": result.get("fraud_indicators", []),
            "agent_reasoning":  result.get("reasoning", ""),
            "agent_tool_calls": result.get("tool_calls_made", []),
            "agent_iterations": result.get("iterations", 0),
            "agent_engine":     result.get("engine", engine),
            "processing_tier":  "tier2",
            "status":           result["decision"] if result["decision"] != "escalate" else "escalated_tier3"
        })
        upsert_check(payload)
        write_audit_log(check_id, "tier2", result["decision"], {**result, "ms": ms, "engine": engine})

        if result["decision"] == "escalate":
            enqueue_message(os.environ["TIER3_QUEUE_NAME"], payload)

        logger.info(f"Tier 2 [{engine}] done: {check_id} | {result['decision']} | {ms}ms")

    except Exception as e:
        logger.error(f"Tier 2 [{engine}] failed {check_id}: {e}", exc_info=True)
        raise
