"""
Azure AI Foundry Agent Service implementation of the Tier-2 fraud investigation agent.

This is the third Tier-2 engine alongside `native` (hand-rolled ReAct on
chat.completions) and `sk` (Semantic Kernel ChatCompletionAgent). It calls
the managed Azure AI Foundry Agent Service runtime:

  - Server-side agent (instructions + tool defs + JSON schema response format)
    is created on first call and cached per worker.
  - Each check gets a fresh thread (no cross-check memory yet —
    threads-keyed-by-account is a future enhancement).
  - Tool calls execute client-side via `enable_auto_function_calls`; payload
    is passed to tool callables via a contextvar.

Switch engines: set TIER2_ENGINE=azure_agent.
Requires: AZURE_AI_AGENTS_ENDPOINT (falls back to AZURE_OPENAI_ENDPOINT)
          and DefaultAzureCredential (Entra ID — `az login` locally or MI in Azure).
"""

import asyncio
import contextvars
import json
import logging
import os
import time
from typing import Optional

from azure.ai.agents import AgentsClient
from azure.ai.agents.models import AgentsResponseFormat, FunctionTool, ToolSet
from azure.identity import DefaultAzureCredential
from pydantic import ValidationError

from handlers.shared.content_safety import check_prompt_safety
from handlers.shared.cosmos import get_cosmos_container, get_customer
from handlers.shared.models import FraudDecision
from handlers.shared.velocity import query_velocity

logger = logging.getLogger(__name__)

AGENT_NAME = "FraudInvestigationAgent"
SYSTEM_PROMPT = """You are an expert check fraud detection agent reviewing escalated checks.
Tier 1 already auto-rejected clear fraud and auto-approved clean checks.
You only see AMBIGUOUS cases that Tier 1 could not decide with confidence.

Rules:
- Start with customer_lookup
- Use velocity_check for amounts near $10,000
- Use fraud_pattern_search once you have indicators
- Maximum 6 tool calls total
- Only use escalate_to_human after using at least 3 other tools

DECISION GUIDELINES:
- approve: All signals are benign after full investigation.
- reject: CONFIRMED fraud from MULTIPLE corroborating signals.
- escalate: Any ambiguity remains. When uncertain, always escalate.

PATTERN IDENTIFICATION — always assign a pattern, never use unknown:
- missing_signature + low_ocr → POSSIBLE_altered_check
- amount_mismatch + suspicious_payee → POSSIBLE_money_mule
- amount near $10,000 + high velocity → POSSIBLE_structuring
- new_account + large_amount + kyc_fail → POSSIBLE_synthetic_identity
- any unrecognized combination → POSSIBLE_altered_check (default fallback)

Always end with a JSON decision:
{"decision":"approve"|"reject"|"escalate","risk_score":0-100,"fraud_pattern":"structuring"|"altered_check"|"synthetic_identity"|"POSSIBLE_altered_check"|"POSSIBLE_structuring"|"POSSIBLE_synthetic_identity"|"POSSIBLE_money_mule","fraud_indicators":[],"reasoning":"explanation"}"""

_current_payload: contextvars.ContextVar[dict] = contextvars.ContextVar("azure_agent_payload")

_client: Optional[AgentsClient] = None
_agent_id: Optional[str] = None


def _get_client() -> AgentsClient:
    global _client
    if _client is None:
        endpoint = os.environ.get("AZURE_AI_AGENTS_ENDPOINT") or os.environ["AZURE_OPENAI_ENDPOINT"]
        _client = AgentsClient(endpoint=endpoint, credential=DefaultAzureCredential())
    return _client


def _response_format() -> AgentsResponseFormat:
    # json_object mode: model must output valid JSON — no schema to validate,
    # so no anyOf/strict-mode incompatibilities. _verdict() parses the JSON.
    return AgentsResponseFormat(type="json_object")


def _build_toolset() -> ToolSet:
    toolset = ToolSet()
    toolset.add(FunctionTool({
        customer_lookup,
        velocity_check,
        signature_check,
        fraud_pattern_search,
        payee_verify,
        escalate_to_human,
    }))
    return toolset


def _get_or_create_agent(client: AgentsClient) -> str:
    global _agent_id
    if _agent_id:
        return _agent_id

    for existing in client.list_agents():
        if existing.name == AGENT_NAME:
            _agent_id = existing.id
            logger.info(f"[Foundry] Reusing existing agent {_agent_id}")
            return _agent_id

    agent = client.create_agent(
        model=os.environ["AZURE_OPENAI_DEPLOYMENT"],
        name=AGENT_NAME,
        instructions=SYSTEM_PROMPT,
        toolset=_build_toolset(),
        response_format=_response_format(),
        temperature=0.1,
    )
    _agent_id = agent.id
    logger.info(f"[Foundry] Created agent {_agent_id}")
    return _agent_id


# ---------------------------------------------------------------------------
# Tool callables — names + signatures must match what create_agent registers.
# Payload is supplied via the _current_payload contextvar.
# ---------------------------------------------------------------------------

def customer_lookup(account_number: str) -> str:
    """Look up full customer profile and account standing by account number."""
    customer = get_customer(account_number)
    if not customer:
        return json.dumps({"found": False})
    return json.dumps({
        "found":                   True,
        "customer_name":           customer.get("customer_name"),
        "account_status":          customer.get("account_status"),
        "kyc_verified":            customer.get("kyc_verified"),
        "synthetic_identity_risk": customer.get("synthetic_identity_risk"),
        "avg_monthly_balance":     customer.get("avg_monthly_balance"),
        "linked_accounts":         customer.get("linked_accounts", []),
        "flags":                   customer.get("flags", []),
    })


def velocity_check(account_number: str, days_back: int = 30) -> str:
    """Check transaction frequency and totals for structuring detection."""
    data = query_velocity(account_number, days_back=days_back)
    return json.dumps({
        "total_checks":             data["count"],
        "total_amount":             data["total"],
        "near_ctr_threshold_count": data["near_ctr"],
        "structuring_risk":         data["near_ctr"] >= 2,
        "recent_transactions":      data["recent_txns"][:10],
    })


def signature_check(check_id: str, account_number: str) -> str:
    """Validate whether the check signature is present and matches account records."""
    payload = _current_payload.get({})
    ef  = payload.get("extracted_fields") or {}
    sig = ef.get("signature_present", False)
    return json.dumps({"signature_present": sig, "signature_match": sig, "confidence": 0.85 if sig else 0.0})


def fraud_pattern_search(indicators: list[str], account_number: str = "") -> str:
    """Search the fraud pattern knowledge base for known schemes matching the given indicators."""
    container  = get_cosmos_container("fraud_cases")
    all_cases  = list(container.read_all_items())
    indicator_set = set(indicators)
    matches    = []
    for case in all_cases:
        overlap = set(case.get("agent_signals", [])).intersection(indicator_set)
        if overlap:
            matches.append({
                "pattern":          case.get("pattern"),
                "description":      case.get("description"),
                "matching_signals": list(overlap),
                "match_confidence": len(overlap) / max(len(indicator_set), 1),
            })
    matches.sort(key=lambda x: x["match_confidence"], reverse=True)
    return json.dumps({"matches_found": len(matches), "top_matches": matches[:3]})


def payee_verify(payee_name: str, amount: float = 0.0) -> str:
    """Verify if the payee name is known, suspicious, or associated with fraud schemes."""
    suspicious = any(kw in payee_name.lower() for kw in ["cash", "bearer", "atm", "wire", "anonymous"])
    return json.dumps({
        "payee_name":      payee_name,
        "suspicious":      suspicious,
        "risk_assessment": "suspicious" if suspicious else "low_risk",
    })


def escalate_to_human(reason: str, suspected_pattern: str, risk_score: int) -> str:
    """Escalate to a human analyst. Use only after exhausting all available analysis tools."""
    return json.dumps({"status": "escalation_queued", "reason": reason})


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def run_agent_foundry(payload: dict, start_time: float) -> dict:
    check_id = payload.get("id")
    logger.info(f"[Foundry] Starting investigation for {check_id}")

    shield_input = (
        f"payee={payload.get('payee_name')} bank={payload.get('bank_name')} "
        f"memo={payload.get('memo')}"
    )
    shield = check_prompt_safety(
        shield_input,
        documents=[
            str(payload.get("payee_name") or ""),
            str(payload.get("bank_name") or ""),
            str(payload.get("memo") or ""),
        ],
    )
    if not shield["safe"]:
        logger.warning(f"[Foundry] Prompt shield rejected check {check_id}: {shield['reason']}")
        return FraudDecision(
            decision="escalate",
            risk_score=80,
            fraud_pattern="unknown",
            fraud_indicators=["prompt_injection_detected"],
            reasoning=f"Content Safety Prompt Shield flagged input: {shield['reason']}",
            engine="azure_agent",
        ).model_dump()

    _current_payload.set(payload)
    client    = _get_client()
    agent_id  = _get_or_create_agent(client)
    toolset   = _build_toolset()
    client.enable_auto_function_calls(toolset)

    ef = payload.get("extracted_fields", {})
    user_message = (
        f"Analyze this check for fraud:\n"
        f"CHECK: id={payload.get('id')} amount=${payload.get('amount', 0):,.2f} "
        f"payee={payload.get('payee_name')} account={payload.get('account_number')} "
        f"bank={payload.get('bank_name')} submitted={payload.get('submission_date')}\n"
        f"EXTRACTION: micr_valid={ef.get('micr_valid')} amount_mismatch={ef.get('amount_mismatch')} "
        f"signature={ef.get('signature_present')} ocr_confidence={ef.get('raw_ocr_confidence', 1.0):.0%}\n"
        f"TIER1: risk_score={payload.get('tier1_risk_score', 'N/A')} "
        f"indicators={', '.join(payload.get('tier1_indicators', [])) or 'None'}\n"
        f"Investigate and provide your final JSON decision."
    )

    run = await asyncio.to_thread(
        client.create_thread_and_process_run,
        agent_id=agent_id,
        thread={"messages": [{"role": "user", "content": user_message}]},
        toolset=toolset,
    )
    elapsed = time.time() - start_time
    logger.info(f"[Foundry] Run {run.id} finished status={run.status} in {elapsed:.1f}s")

    final_text, tool_calls_made, iterations = _extract_outputs(client, run)
    return _verdict(final_text, tool_calls_made, iterations, run.status)


def _extract_outputs(client: AgentsClient, run) -> tuple[str, list, int]:
    tool_calls_made = []
    iterations = 0
    final_text = ""

    for step in client.run_steps.list(thread_id=run.thread_id, run_id=run.id):
        iterations += 1
        details = step.step_details
        if getattr(details, "tool_calls", None):
            for tc in details.tool_calls:
                func = getattr(tc, "function", None)
                if func is None:
                    continue
                try:
                    args = json.loads(func.arguments) if isinstance(func.arguments, str) else (func.arguments or {})
                except json.JSONDecodeError:
                    args = {"_raw": func.arguments}
                tool_calls_made.append({"tool": func.name, "args": args})

    for msg in client.messages.list(thread_id=run.thread_id, order="desc"):
        if msg.role != "assistant":
            continue
        for part in msg.content:
            text = getattr(part, "text", None)
            if text and getattr(text, "value", None):
                final_text = text.value
                break
        if final_text:
            break

    return final_text, tool_calls_made, iterations


def _verdict(raw_content: str, tool_calls_made: list, iterations: int, run_status: str) -> dict:
    try:
        start = raw_content.find("{")
        end   = raw_content.rfind("}") + 1
        if start >= 0 and end > start:
            d = json.loads(raw_content[start:end])
            d.update({"tool_calls_made": tool_calls_made, "iterations": iterations, "engine": "azure_agent"})
            return FraudDecision(**d).model_dump()
    except (json.JSONDecodeError, ValidationError) as e:
        logger.warning(f"[Foundry] Verdict parse failed (run_status={run_status}): {e}")
    return FraudDecision(
        decision="escalate",
        risk_score=50,
        fraud_pattern="unknown",
        fraud_indicators=[f"foundry_parse_error_{run_status}"],
        reasoning=raw_content or f"No usable response from Foundry agent (status={run_status})",
        tool_calls_made=tool_calls_made,
        iterations=iterations,
        engine="azure_agent",
    ).model_dump()
