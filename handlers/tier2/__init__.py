import os, json, time, logging
import azure.functions as func
import azure.durable_functions as df

from handlers.shared.cosmos import upsert_check, write_audit_log
from handlers.shared.models import FraudDecision
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
            raw_result = await run_agent_sk(payload, start)
        elif engine == "azure_agent":
            from handlers.tier2.azure_agent import run_agent_foundry
            raw_result = await run_agent_foundry(payload, start)
        else:
            from handlers.tier2.native import run_agent_native
            raw_result = run_agent_native(payload, start)

        decision = FraudDecision(**raw_result)
        result   = decision.model_dump()

        ms = int((time.time() - start) * 1000)
        payload.update({
            "risk_score":       decision.risk_score,
            "fraud_decision":   decision.decision,
            "fraud_pattern":    decision.fraud_pattern,
            "fraud_indicators": decision.fraud_indicators,
            "agent_reasoning":  decision.reasoning,
            "agent_tool_calls": result["tool_calls_made"],
            "agent_iterations": decision.iterations,
            "agent_engine":     decision.engine,
            "processing_tier":  "tier2",
            "status":           decision.decision if decision.decision != "escalate" else "escalated_tier3"
        })
        upsert_check(payload)
        write_audit_log(check_id, "tier2", decision.decision, {**result, "ms": ms, "engine": engine})

        if decision.decision == "escalate":
            enqueue_message(os.environ["TIER3_QUEUE_NAME"], payload)

        logger.info(f"Tier 2 [{engine}] done: {check_id} | {decision.decision} | {ms}ms")

    except Exception as e:
        logger.error(f"Tier 2 [{engine}] failed {check_id}: {e}", exc_info=True)
        raise
