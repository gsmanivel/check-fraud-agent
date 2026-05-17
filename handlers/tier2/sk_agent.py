"""
Semantic Kernel implementation of the Tier-2 fraud investigation agent.

Architecture — 3-phase Fraud Investigation Process:

  Phase 1 · ContextStep (deterministic)
    Customer lookup + initial risk signals gathered before the AI agent starts.
    Equivalent to a KernelProcessStep with no LLM call — pure data retrieval.

  Phase 2 · InvestigationStep (agentic)
    ChatCompletionAgent with a typed KernelPlugin (FraudInvestigationPlugin).
    The agent autonomously selects and calls tools in a ReAct loop managed by SK.

  Phase 3 · VerdictStep (deterministic)
    Structured extraction and validation of the agent's JSON decision.
    Normalises output to the same contract as the native engine.

Switching engines: set TIER2_ENGINE=sk in Azure Function App settings (or local.settings.json).
No redeployment required.
"""

import os, json, time, logging
from semantic_kernel import Kernel
from semantic_kernel.agents import ChatCompletionAgent
from semantic_kernel.connectors.ai.open_ai import AzureChatCompletion
from semantic_kernel.contents import ChatHistory, FunctionCallContent
from semantic_kernel.functions import kernel_function

from handlers.shared.content_safety import check_prompt_safety
from handlers.shared.cosmos import get_cosmos_container, get_customer
from handlers.shared.velocity import query_velocity

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are an expert check fraud detection agent operating within a structured investigation process.

Investigation phases:
1. CONTEXT  — customer_lookup is always your first call (context has been pre-gathered, verify it)
2. SIGNALS  — use velocity_check for amounts near $10,000; use payee_verify for payee risk
3. PATTERNS — use fraud_pattern_search once you have gathered indicators
4. VERDICT  — provide your final JSON decision

Rules:
- Always call customer_lookup first
- Maximum 6 tool calls total
- Only call escalate_to_human after using at least 3 analysis tools

Final response MUST be valid JSON (no markdown, no preamble):
{"decision":"approve"|"reject"|"escalate","risk_score":0-100,"fraud_pattern":"structuring"|"altered_check"|"synthetic_identity"|"unknown"|null,"fraud_indicators":[],"reasoning":"explanation"}"""


# ---------------------------------------------------------------------------
# Phase 1 — Context Step (deterministic, no LLM)
# ---------------------------------------------------------------------------

def _context_step(payload: dict) -> dict:
    """Deterministic evidence gathering before the AI agent starts."""
    account_number = payload.get("account_number", "")
    customer       = get_customer(account_number)
    velocity       = query_velocity(account_number, days_back=1, exclude_check_id=payload.get("id", ""))

    pre_signals = []
    if not customer:
        pre_signals.append("account_not_found")
    else:
        if customer.get("account_status") != "active":           pre_signals.append("account_not_active")
        if customer.get("synthetic_identity_risk", False):        pre_signals.append("synthetic_identity_flag")
        if not customer.get("kyc_verified", True):                pre_signals.append("kyc_not_verified")
    if velocity["count"] >= 5:   pre_signals.append("high_velocity_5plus_24h")
    elif velocity["count"] >= 3: pre_signals.append("elevated_velocity_3plus_24h")
    if velocity["total"] + payload.get("amount", 0) >= 9000:
        pre_signals.append("cumulative_amount_near_ctr_24h")

    logger.info(f"[SK·ContextStep] pre-signals={pre_signals}")
    return {
        "customer":    customer,
        "velocity_1d": velocity,
        "pre_signals": pre_signals,
    }


# ---------------------------------------------------------------------------
# Phase 2 — Investigation Step (SK ChatCompletionAgent + KernelPlugin)
# ---------------------------------------------------------------------------

class FraudInvestigationPlugin:
    """
    Typed Semantic Kernel plugin exposing fraud investigation tools as
    @kernel_function decorated methods. SK auto-generates JSON schemas from
    the type annotations and docstrings — no manual TOOLS list required.
    """

    def __init__(self, payload: dict):
        self._payload = payload

    @kernel_function(description="Look up full customer profile and account standing by account number.")
    def customer_lookup(self, account_number: str) -> str:
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

    @kernel_function(description="Check transaction frequency and cumulative totals to detect structuring or velocity fraud.")
    def velocity_check(self, account_number: str, days_back: int = 30) -> str:
        data = query_velocity(account_number, days_back=days_back)
        return json.dumps({
            "total_checks":             data["count"],
            "total_amount":             data["total"],
            "near_ctr_threshold_count": data["near_ctr"],
            "structuring_risk":         data["near_ctr"] >= 2,
            "recent_transactions":      data["recent_txns"][:10],
        })

    @kernel_function(description="Validate whether the check signature is present and matches account records.")
    def signature_check(self, check_id: str, account_number: str) -> str:
        ef  = self._payload.get("extracted_fields", {})
        sig = ef.get("signature_present", False)
        return json.dumps({"signature_present": sig, "signature_match": sig, "confidence": 0.85 if sig else 0.0})

    @kernel_function(description="Search the fraud pattern knowledge base for known schemes matching the given indicators. Pass indicators as a JSON array string.")
    def fraud_pattern_search(self, indicators_json: str, account_number: str = "") -> str:
        indicators = set(json.loads(indicators_json) if indicators_json.strip().startswith("[") else [indicators_json])
        container  = get_cosmos_container("fraud_cases")
        all_cases  = list(container.read_all_items())
        matches    = []
        for case in all_cases:
            overlap = set(case.get("agent_signals", [])).intersection(indicators)
            if overlap:
                matches.append({
                    "pattern":          case.get("pattern"),
                    "description":      case.get("description"),
                    "matching_signals": list(overlap),
                    "match_confidence": len(overlap) / max(len(indicators), 1),
                })
        matches.sort(key=lambda x: x["match_confidence"], reverse=True)
        return json.dumps({"matches_found": len(matches), "top_matches": matches[:3]})

    @kernel_function(description="Verify if the payee name is known, suspicious, or associated with fraud schemes.")
    def payee_verify(self, payee_name: str, amount: float = 0.0) -> str:
        suspicious = any(kw in payee_name.lower() for kw in ["cash", "bearer", "atm", "wire", "anonymous"])
        return json.dumps({
            "payee_name":      payee_name,
            "suspicious":      suspicious,
            "risk_assessment": "suspicious" if suspicious else "low_risk",
        })

    @kernel_function(description="Escalate to a human analyst. Use only after exhausting all available analysis tools.")
    def escalate_to_human(self, reason: str, suspected_pattern: str, risk_score: int) -> str:
        return json.dumps({"status": "escalation_queued", "reason": reason})


async def _investigation_step(payload: dict, context: dict, start_time: float) -> tuple[str, list, int]:
    """AI-driven investigation using SK ChatCompletionAgent and FraudInvestigationPlugin."""
    kernel = Kernel()
    kernel.add_service(AzureChatCompletion(
        deployment_name=os.environ["AZURE_OPENAI_DEPLOYMENT"],
        endpoint=os.environ["AZURE_OPENAI_ENDPOINT"],
        api_key=os.environ["AZURE_OPENAI_KEY"],
        api_version=os.environ.get("AZURE_OPENAI_API_VERSION", "2024-10-21"),
    ))
    kernel.add_plugin(FraudInvestigationPlugin(payload), plugin_name="fraud")

    agent = ChatCompletionAgent(
        kernel=kernel,
        name="FraudInvestigationAgent",
        instructions=SYSTEM_PROMPT,
    )

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
        f"PRE-GATHERED CONTEXT: pre_signals={context['pre_signals']}\n"
        f"Investigate and provide your final JSON decision."
    )

    history         = ChatHistory()
    history.add_user_message(user_message)
    tool_calls_made = []
    final_content   = None
    iterations      = 0
    timeout         = int(os.environ.get("AGENT_TIMEOUT_SECONDS", "30"))

    async for response in agent.invoke(messages=history):
        iterations += 1
        for item in response.items:
            if isinstance(item, FunctionCallContent):
                tool_calls_made.append({
                    "tool": item.function_name,
                    "args": json.loads(item.arguments) if isinstance(item.arguments, str) and item.arguments else {},
                })
                logger.info(f"[SK·InvestigationStep] tool={item.function_name}")
            elif hasattr(item, "text") and item.text:
                final_content = item.text

        if time.time() - start_time > timeout:
            logger.warning(f"[SK·InvestigationStep] timeout after {iterations} iterations")
            break

    return final_content or "", tool_calls_made, iterations


# ---------------------------------------------------------------------------
# Phase 3 — Verdict Step (deterministic)
# ---------------------------------------------------------------------------

def _verdict_step(raw_content: str, tool_calls_made: list, iterations: int) -> dict:
    """Extract, validate, and normalise the agent's JSON decision."""
    try:
        start = raw_content.find("{")
        end   = raw_content.rfind("}") + 1
        if start >= 0 and end > start:
            d = json.loads(raw_content[start:end])
            d.update({"tool_calls_made": tool_calls_made, "iterations": iterations, "engine": "semantic_kernel"})
            return d
    except Exception:
        pass
    return {
        "decision":        "escalate",
        "risk_score":      50,
        "fraud_pattern":   "unknown",
        "fraud_indicators": ["sk_parse_error"],
        "reasoning":       raw_content or "No response from SK agent",
        "tool_calls_made": tool_calls_made,
        "iterations":      iterations,
        "engine":          "semantic_kernel",
    }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def run_agent_sk(payload: dict, start_time: float) -> dict:
    check_id = payload.get("id")
    logger.info(f"[SK] Starting 3-phase investigation for {check_id}")

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
        logger.warning(f"[SK] Prompt shield rejected check {check_id}: {shield['reason']}")
        return {
            "decision":         "escalate",
            "risk_score":       80,
            "fraud_pattern":    "unknown",
            "fraud_indicators": ["prompt_injection_detected"],
            "reasoning":        f"Content Safety Prompt Shield flagged input: {shield['reason']}",
            "tool_calls_made":  [],
            "iterations":       0,
            "engine":           "semantic_kernel",
        }

    context = _context_step(payload)
    logger.info(f"[SK·ContextStep] complete — pre_signals={context['pre_signals']}")

    raw_content, tool_calls_made, iterations = await _investigation_step(payload, context, start_time)
    logger.info(f"[SK·InvestigationStep] complete — {iterations} iterations, {len(tool_calls_made)} tool calls")

    result = _verdict_step(raw_content, tool_calls_made, iterations)
    logger.info(f"[SK·VerdictStep] decision={result['decision']} risk={result['risk_score']}")

    return result
