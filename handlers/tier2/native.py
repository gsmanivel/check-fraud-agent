import os, json, time, logging
from openai import AzureOpenAI

from handlers.shared.cosmos import get_cosmos_container, get_customer
from handlers.shared.velocity import query_velocity

logger = logging.getLogger(__name__)

MAX_ITERATIONS  = int(os.environ.get("AGENT_MAX_ITERATIONS", "6"))
TIMEOUT_SECONDS = int(os.environ.get("AGENT_TIMEOUT_SECONDS", "30"))

TOOLS = [
    {"type": "function", "function": {"name": "customer_lookup",      "description": "Look up full customer profile.",                                          "parameters": {"type": "object", "properties": {"account_number": {"type": "string"}},                                                                                                                                                                  "required": ["account_number"]}}},
    {"type": "function", "function": {"name": "velocity_check",       "description": "Check transaction frequency and totals for structuring detection.",      "parameters": {"type": "object", "properties": {"account_number": {"type": "string"}, "days_back": {"type": "integer", "default": 30}},                                                                                                            "required": ["account_number"]}}},
    {"type": "function", "function": {"name": "signature_check",      "description": "Validate check signature.",                                              "parameters": {"type": "object", "properties": {"check_id": {"type": "string"}, "account_number": {"type": "string"}},                                                                                                                            "required": ["check_id", "account_number"]}}},
    {"type": "function", "function": {"name": "fraud_pattern_search", "description": "Search fraud pattern knowledge base.",                                   "parameters": {"type": "object", "properties": {"indicators": {"type": "array", "items": {"type": "string"}}, "account_number": {"type": "string"}},                                                                                              "required": ["indicators"]}}},
    {"type": "function", "function": {"name": "payee_verify",         "description": "Verify if payee is known or suspicious.",                                "parameters": {"type": "object", "properties": {"payee_name": {"type": "string"}, "amount": {"type": "number"}},                                                                                                                                  "required": ["payee_name"]}}},
    {"type": "function", "function": {"name": "escalate_to_human",    "description": "Escalate to human analyst. Use only after exhausting other tools.",      "parameters": {"type": "object", "properties": {"reason": {"type": "string"}, "suspected_pattern": {"type": "string", "enum": ["structuring", "altered_check", "synthetic_identity", "unknown"]}, "risk_score": {"type": "integer"}}, "required": ["reason", "suspected_pattern", "risk_score"]}}},
]

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

def run_agent_native(payload: dict, start_time: float) -> dict:
    client = AzureOpenAI(
        azure_endpoint=os.environ["AZURE_OPENAI_ENDPOINT"],
        api_key=os.environ["AZURE_OPENAI_KEY"],
        api_version="2024-08-01-preview"
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
        f"Investigate and provide your final JSON decision."
    )
    messages        = [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": user_message}]
    tool_calls_made = []
    iterations      = 0
    escalation      = None

    while iterations < MAX_ITERATIONS:
        if time.time() - start_time > TIMEOUT_SECONDS:
            logger.warning(f"Native agent timeout for {payload.get('id')} after {iterations} iterations")
            break
        iterations += 1
        response = client.chat.completions.create(
            model=os.environ["AZURE_OPENAI_DEPLOYMENT"],
            messages=messages, tools=TOOLS, tool_choice="auto",
            temperature=0.1, max_tokens=2000
        )
        message = response.choices[0].message
        if not message.tool_calls:
            result = _parse_decision(message.content, tool_calls_made, iterations)
            result["engine"] = "native"
            return result
        messages.append({"role": "assistant", "content": message.content, "tool_calls": [
            {"id": tc.id, "type": "function", "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
            for tc in message.tool_calls
        ]})
        for tc in message.tool_calls:
            name   = tc.function.name
            args   = json.loads(tc.function.arguments)
            tool_calls_made.append({"tool": name, "args": args})
            result = _exec_tool(name, args, payload)
            if name == "escalate_to_human":
                escalation = args
            messages.append({"role": "tool", "tool_call_id": tc.id, "content": json.dumps(result)})

    if escalation:
        return {
            "decision": "escalate", "risk_score": escalation.get("risk_score", 60),
            "fraud_pattern": escalation.get("suspected_pattern", "unknown"),
            "fraud_indicators": [], "reasoning": escalation.get("reason", ""),
            "tool_calls_made": tool_calls_made, "iterations": iterations, "engine": "native"
        }
    return {
        "decision": "escalate", "risk_score": 50, "fraud_pattern": "unknown",
        "fraud_indicators": ["max_iterations_reached"], "reasoning": "Agent reached iteration limit",
        "tool_calls_made": tool_calls_made, "iterations": iterations, "engine": "native"
    }


def _exec_tool(name: str, args: dict, payload: dict) -> dict:
    try:
        if name == "customer_lookup":
            customer = get_customer(args["account_number"])
            if not customer:
                return {"found": False}
            return {
                "found": True, "customer_name": customer.get("customer_name"),
                "account_status": customer.get("account_status"), "kyc_verified": customer.get("kyc_verified"),
                "synthetic_identity_risk": customer.get("synthetic_identity_risk"),
                "avg_monthly_balance": customer.get("avg_monthly_balance"),
                "linked_accounts": customer.get("linked_accounts", []), "flags": customer.get("flags", [])
            }
        elif name == "velocity_check":
            data = query_velocity(args["account_number"], days_back=args.get("days_back", 30))
            return {"total_checks": data["count"], "total_amount": data["total"], "near_ctr_threshold_count": data["near_ctr"], "structuring_risk": data["near_ctr"] >= 2, "recent_transactions": data["recent_txns"][:10]}
        elif name == "signature_check":
            ef  = payload.get("extracted_fields", {})
            sig = ef.get("signature_present", False)
            return {"signature_present": sig, "signature_match": sig, "confidence": 0.85 if sig else 0.0}
        elif name == "fraud_pattern_search":
            container = get_cosmos_container("fraud_cases")
            all_cases = list(container.read_all_items())
            indicator_set = set(i.lower() for i in args.get("indicators", []))
            matches       = []
            for case in all_cases:
                case_signals = set(s.lower() for s in case.get("agent_signals", []))
                overlap      = case_signals.intersection(indicator_set)
                if overlap:
                    matches.append({
                        "pattern": case.get("pattern"), "description": case.get("description"),
                        "matching_signals": list(overlap),
                        "match_confidence": len(overlap) / max(len(indicator_set), 1)
                    })
            matches.sort(key=lambda x: x["match_confidence"], reverse=True)
            return {"matches_found": len(matches), "top_matches": matches[:3]}
        elif name == "payee_verify":
            payee = args.get("payee_name", "").lower()
            suspicious_keywords = ["cash", "bearer", "atm", "wire", "anonymous"]
            is_suspicious = any(k in payee for k in suspicious_keywords)
            return {
                "payee_name": args.get("payee_name"), "suspicious": is_suspicious,
                "risk_assessment": "high_risk" if is_suspicious else "low_risk"
            }
        elif name == "escalate_to_human":
            return {
                "status": "escalation_queued",
                "reason": args.get("reason"),
                "suspected_pattern": args.get("suspected_pattern"),
                "risk_score": args.get("risk_score")
            }
    except Exception as e:
        logger.error(f"Tool {name} failed: {e}", exc_info=True)
        return {"error": str(e)}
    return {"error": "unknown_tool"}


def _parse_decision(content: str, tool_calls_made: list, iterations: int) -> dict:
    try:
        start = content.find("{")
        end   = content.rfind("}") + 1
        if start >= 0 and end > start:
            data = json.loads(content[start:end])
            data["tool_calls_made"] = tool_calls_made
            data["iterations"]      = iterations
            return data
    except Exception as e:
        logger.warning(f"Could not parse decision JSON: {e}")
    return {
        "decision": "escalate", "risk_score": 50, "fraud_pattern": "unknown",
        "fraud_indicators": ["parse_error"], "reasoning": content or "No content",
        "tool_calls_made": tool_calls_made, "iterations": iterations
    }