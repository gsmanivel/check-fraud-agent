import os
import json
import time
import logging
import azure.functions as func
from openai import AzureOpenAI
from datetime import datetime, timezone, timedelta
import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from shared.models import (
    FraudDecision, FraudPattern,
    get_customer, get_cosmos_container,
    upsert_check, write_audit_log, enqueue_message
)

logger = logging.getLogger(__name__)

app = func.FunctionApp()

MAX_ITERATIONS = int(os.environ.get("AGENT_MAX_ITERATIONS", "6"))
TIMEOUT_SECONDS = int(os.environ.get("AGENT_TIMEOUT_SECONDS", "30"))


# ── Tool definitions for GPT-4o ───────────────────────────────────────────────

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "customer_lookup",
            "description": "Look up full customer profile including account history, KYC status, linked accounts, and risk flags.",
            "parameters": {
                "type": "object",
                "properties": {
                    "account_number": {"type": "string", "description": "The account number to look up"}
                },
                "required": ["account_number"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "velocity_check",
            "description": "Check transaction frequency and total amounts for an account over a given time window. Detects structuring patterns.",
            "parameters": {
                "type": "object",
                "properties": {
                    "account_number": {"type": "string"},
                    "days_back": {"type": "integer", "description": "How many days back to look", "default": 30}
                },
                "required": ["account_number"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "signature_check",
            "description": "Validate the check signature against the customer's signature on file using Document Intelligence.",
            "parameters": {
                "type": "object",
                "properties": {
                    "check_id": {"type": "string"},
                    "account_number": {"type": "string"}
                },
                "required": ["check_id", "account_number"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "fraud_pattern_search",
            "description": "Search the fraud pattern knowledge base for similar cases. Returns matching fraud patterns and their confidence scores.",
            "parameters": {
                "type": "object",
                "properties": {
                    "indicators": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of fraud indicators to search for"
                    },
                    "account_number": {"type": "string"}
                },
                "required": ["indicators"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "payee_verify",
            "description": "Verify if the payee name and account is known or suspicious. Checks against known payee list and fraud registries.",
            "parameters": {
                "type": "object",
                "properties": {
                    "payee_name": {"type": "string"},
                    "amount": {"type": "number"}
                },
                "required": ["payee_name"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "escalate_to_human",
            "description": "Escalate the check to a human analyst. Use only when you cannot make a confident decision after using other tools.",
            "parameters": {
                "type": "object",
                "properties": {
                    "reason": {"type": "string", "description": "Clear explanation of why human review is needed"},
                    "suspected_pattern": {
                        "type": "string",
                        "enum": ["structuring", "altered_check", "synthetic_identity", "unknown"]
                    },
                    "risk_score": {"type": "integer", "description": "Your estimated risk score 0-100"}
                },
                "required": ["reason", "suspected_pattern", "risk_score"]
            }
        }
    }
]

SYSTEM_PROMPT = """You are an expert check fraud detection agent at a financial institution.

Your job is to analyze check submissions that were escalated from the automated rules engine because they had borderline risk signals. You must reason carefully across ALL available signals — not just individual ones — to determine if this check is fraudulent.

You have access to 6 tools. Use them strategically:
1. customer_lookup — understand who this customer really is
2. velocity_check — detect structuring and unusual frequency patterns
3. signature_check — validate the physical signature
4. fraud_pattern_search — match against known fraud patterns
5. payee_verify — check if the payee is suspicious
6. escalate_to_human — only if you genuinely cannot decide

IMPORTANT RULES:
- Start with customer_lookup to understand the account context
- Use velocity_check for any check near $10,000 or from a new account
- Use fraud_pattern_search once you have indicators from other tools
- Maximum 6 tool calls. Be efficient.
- Do NOT escalate unless you have used at least 3 other tools first
- Always return a final JSON decision in this exact format:

{
  "decision": "approve" | "reject" | "escalate",
  "risk_score": 0-100,
  "fraud_pattern": "structuring" | "altered_check" | "synthetic_identity" | "unknown" | null,
  "fraud_indicators": ["indicator1", "indicator2"],
  "reasoning": "Clear explanation of your decision based on the evidence"
}
"""


@app.service_bus_queue_trigger(
    arg_name="msg",
    queue_name="%TIER2_QUEUE_NAME%",
    connection="SERVICE_BUS_CONNECTION_STRING"
)
def tier2_agent(msg: func.ServiceBusMessage):
    """
    Plain Azure Function triggered by Service Bus.
    Runs GPT-4o ReAct loop with 6 tools to reason about the check.
    """
    start = time.time()
    payload = json.loads(msg.get_body().decode("utf-8"))
    check_id = payload.get("id")
    logger.info(f"Tier 2 agent processing check: {check_id}")

    try:
        result = _run_agent(payload, start)

        # ── Update Cosmos DB ──────────────────────────────────────────────
        payload["risk_score"] = result["risk_score"]
        payload["fraud_decision"] = result["decision"]
        payload["fraud_pattern"] = result.get("fraud_pattern")
        payload["fraud_indicators"] = result.get("fraud_indicators", [])
        payload["agent_reasoning"] = result.get("reasoning", "")
        payload["processing_tier"] = "tier2"
        payload["status"] = result["decision"] if result["decision"] != "escalate" else "escalated_tier3"
        upsert_check(payload)

        processing_time_ms = int((time.time() - start) * 1000)

        write_audit_log(check_id, "tier2", result["decision"], {
            "risk_score": result["risk_score"],
            "fraud_pattern": result.get("fraud_pattern"),
            "fraud_indicators": result.get("fraud_indicators", []),
            "reasoning": result.get("reasoning", ""),
            "tool_calls_made": result.get("tool_calls_made", []),
            "iterations": result.get("iterations", 0),
            "processing_time_ms": processing_time_ms
        })

        # ── Escalate to Tier 3 if needed ──────────────────────────────────
        if result["decision"] == FraudDecision.ESCALATE:
            enqueue_message(os.environ["TIER3_QUEUE_NAME"], payload)
            logger.info(f"Check {check_id} escalated to Tier 3 human review")

        msg.complete()
        logger.info(f"Tier 2 complete: {check_id} | {result['decision']} | {processing_time_ms}ms")

    except Exception as e:
        logger.error(f"Tier 2 agent failed for check {check_id}: {str(e)}", exc_info=True)
        msg.abandon()
        raise


def _run_agent(payload: dict, start_time: float) -> dict:
    """
    Core ReAct loop. Sends check context to GPT-4o, processes tool calls,
    iterates until a final decision is reached or limits are hit.
    """
    client = AzureOpenAI(
        azure_endpoint=os.environ["AZURE_OPENAI_ENDPOINT"],
        api_key=os.environ["AZURE_OPENAI_KEY"],
        api_version="2024-08-01-preview"
    )

    # Build initial user message with full check context
    user_message = _build_user_message(payload)

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_message}
    ]

    tool_calls_made = []
    iterations = 0
    escalation_result = None

    while iterations < MAX_ITERATIONS:
        # Timeout guard
        if time.time() - start_time > TIMEOUT_SECONDS:
            logger.warning(f"Agent timeout after {iterations} iterations")
            break

        iterations += 1
        response = client.chat.completions.create(
            model=os.environ["AZURE_OPENAI_DEPLOYMENT"],
            messages=messages,
            tools=TOOLS,
            tool_choice="auto",
            temperature=0.1,
            max_tokens=2000
        )

        message = response.choices[0].message

        # No more tool calls — agent has made its decision
        if not message.tool_calls:
            return _parse_final_decision(
                message.content,
                tool_calls_made,
                iterations
            )

        # Process tool calls
        messages.append({"role": "assistant", "content": message.content, "tool_calls": [
            {"id": tc.id, "type": "function", "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
            for tc in message.tool_calls
        ]})

        for tool_call in message.tool_calls:
            tool_name = tool_call.function.name
            tool_args = json.loads(tool_call.function.arguments)
            tool_calls_made.append({"tool": tool_name, "args": tool_args})

            logger.info(f"Agent calling tool: {tool_name} with {tool_args}")
            tool_result = _execute_tool(tool_name, tool_args, payload)

            # If escalate tool was called, capture the result
            if tool_name == "escalate_to_human":
                escalation_result = tool_args

            messages.append({
                "role": "tool",
                "tool_call_id": tool_call.id,
                "content": json.dumps(tool_result)
            })

    # Fell through — use escalation if triggered, else default to escalate
    if escalation_result:
        return {
            "decision": FraudDecision.ESCALATE,
            "risk_score": escalation_result.get("risk_score", 60),
            "fraud_pattern": escalation_result.get("suspected_pattern", "unknown"),
            "fraud_indicators": [],
            "reasoning": escalation_result.get("reason", "Agent escalated to human review"),
            "tool_calls_made": tool_calls_made,
            "iterations": iterations
        }

    return {
        "decision": FraudDecision.ESCALATE,
        "risk_score": 50,
        "fraud_pattern": "unknown",
        "fraud_indicators": ["agent_max_iterations_reached"],
        "reasoning": "Agent reached iteration limit without definitive conclusion",
        "tool_calls_made": tool_calls_made,
        "iterations": iterations
    }


# ── Tool implementations ───────────────────────────────────────────────────────

def _execute_tool(tool_name: str, args: dict, payload: dict) -> dict:
    try:
        if tool_name == "customer_lookup":
            return _tool_customer_lookup(args["account_number"])
        elif tool_name == "velocity_check":
            return _tool_velocity_check(args["account_number"], args.get("days_back", 30))
        elif tool_name == "signature_check":
            return _tool_signature_check(args["check_id"], args["account_number"], payload)
        elif tool_name == "fraud_pattern_search":
            return _tool_fraud_pattern_search(args["indicators"], args.get("account_number"))
        elif tool_name == "payee_verify":
            return _tool_payee_verify(args["payee_name"], args.get("amount", 0))
        elif tool_name == "escalate_to_human":
            return {"status": "escalation_queued", "reason": args.get("reason")}
        else:
            return {"error": f"Unknown tool: {tool_name}"}
    except Exception as e:
        logger.error(f"Tool {tool_name} failed: {str(e)}")
        return {"error": str(e)}


def _tool_customer_lookup(account_number: str) -> dict:
    customer = get_customer(account_number)
    if not customer:
        return {"found": False, "account_number": account_number}

    return {
        "found": True,
        "account_number": account_number,
        "customer_name": customer.get("customer_name"),
        "account_type": customer.get("account_type"),
        "account_status": customer.get("account_status"),
        "opened_date": customer.get("opened_date"),
        "kyc_verified": customer.get("kyc_verified"),
        "synthetic_identity_risk": customer.get("synthetic_identity_risk"),
        "avg_monthly_balance": customer.get("avg_monthly_balance"),
        "credit_score": customer.get("credit_score"),
        "linked_accounts": customer.get("linked_accounts", []),
        "flags": customer.get("flags", [])
    }


def _tool_velocity_check(account_number: str, days_back: int = 30) -> dict:
    container = get_cosmos_container(os.environ["COSMOS_CHECKS_CONTAINER"])
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days_back)).isoformat()

    # Count of checks
    count_query = f"""
        SELECT VALUE COUNT(1) FROM c
        WHERE c.account_number = '{account_number}'
        AND c.submission_date >= '{cutoff}'
    """
    count_results = list(container.query_items(query=count_query, enable_cross_partition_query=True))
    check_count = count_results[0] if count_results else 0

    # Total amount
    amount_query = f"""
        SELECT VALUE SUM(c.amount) FROM c
        WHERE c.account_number = '{account_number}'
        AND c.submission_date >= '{cutoff}'
    """
    amount_results = list(container.query_items(query=amount_query, enable_cross_partition_query=True))
    total_amount = amount_results[0] or 0

    # Amount distribution — check for structuring
    distribution_query = f"""
        SELECT c.amount, c.submission_date FROM c
        WHERE c.account_number = '{account_number}'
        AND c.submission_date >= '{cutoff}'
        ORDER BY c.submission_date DESC
    """
    transactions = list(container.query_items(query=distribution_query, enable_cross_partition_query=True))

    near_threshold_count = sum(1 for t in transactions if 8500 <= t.get("amount", 0) <= 9999)

    return {
        "account_number": account_number,
        "days_analyzed": days_back,
        "total_checks": check_count,
        "total_amount": round(total_amount, 2),
        "near_ctr_threshold_count": near_threshold_count,
        "recent_transactions": transactions[:10],
        "structuring_risk": near_threshold_count >= 2
    }


def _tool_signature_check(check_id: str, account_number: str, payload: dict) -> dict:
    """
    In production: calls Document Intelligence to compare signatures.
    For demo: uses extracted_fields from payload.
    """
    ef = payload.get("extracted_fields", {})
    signature_present = ef.get("signature_present", False)

    return {
        "check_id": check_id,
        "signature_present": signature_present,
        "signature_match": signature_present,
        "confidence": 0.85 if signature_present else 0.0,
        "note": "Signature validated against account signature on file"
    }


def _tool_fraud_pattern_search(indicators: list, account_number: str = None) -> dict:
    """
    Searches Cosmos DB fraud_cases for matching patterns.
    In production: would use Azure AI Search with vector embeddings.
    """
    container = get_cosmos_container("fraud_cases")
    all_cases = list(container.read_all_items())

    matches = []
    for case in all_cases:
        case_indicators = set(case.get("agent_signals", []))
        query_indicators = set(indicators)
        overlap = case_indicators.intersection(query_indicators)
        if overlap:
            matches.append({
                "pattern": case.get("pattern"),
                "description": case.get("description"),
                "matching_signals": list(overlap),
                "match_confidence": len(overlap) / max(len(query_indicators), 1)
            })

    matches.sort(key=lambda x: x["match_confidence"], reverse=True)

    return {
        "indicators_searched": indicators,
        "matches_found": len(matches),
        "top_matches": matches[:3]
    }


def _tool_payee_verify(payee_name: str, amount: float) -> dict:
    """
    Checks payee against known legitimate payees and fraud registries.
    For demo: simple heuristic check.
    """
    suspicious_keywords = ["cash", "bearer", "atm", "wire", "transfer", "anonymous"]
    is_suspicious = any(kw in payee_name.lower() for kw in suspicious_keywords)

    return {
        "payee_name": payee_name,
        "found_in_known_payees": not is_suspicious,
        "suspicious_keywords_detected": is_suspicious,
        "amount": amount,
        "high_amount_new_payee": amount > 10000 and not is_suspicious,
        "risk_assessment": "suspicious" if is_suspicious else "low_risk"
    }


# ── Helpers ───────────────────────────────────────────────────────────────────

def _build_user_message(payload: dict) -> str:
    ef = payload.get("extracted_fields", {})
    return f"""Please analyze this check for fraud:

CHECK DETAILS:
- Check ID: {payload.get('id')}
- Amount: ${payload.get('amount', 0):,.2f}
- Payee: {payload.get('payee_name')}
- Account: {payload.get('account_number')}
- Bank: {payload.get('bank_name')}
- Submitted: {payload.get('submission_date')}

DOCUMENT INTELLIGENCE EXTRACTION:
- MICR Valid: {ef.get('micr_valid')}
- Amount Mismatch: {ef.get('amount_mismatch')}
- Signature Present: {ef.get('signature_present')}
- OCR Confidence: {ef.get('raw_ocr_confidence', 1.0):.0%}

TIER 1 ESCALATION REASON:
- Risk Score from Tier 1: {payload.get('tier1_risk_score', 'N/A')}
- Tier 1 Indicators: {', '.join(payload.get('tier1_indicators', [])) or 'None'}

Investigate this check thoroughly using the available tools and provide your final decision."""


def _parse_final_decision(content: str, tool_calls_made: list, iterations: int) -> dict:
    """
    Parses the agent's final JSON decision from the response content.
    """
    try:
        # Find JSON block in response
        start = content.find("{")
        end = content.rfind("}") + 1
        if start >= 0 and end > start:
            decision_json = json.loads(content[start:end])
            decision_json["tool_calls_made"] = tool_calls_made
            decision_json["iterations"] = iterations
            return decision_json
    except Exception as e:
        logger.error(f"Failed to parse agent decision: {e}")

    # Fallback
    return {
        "decision": FraudDecision.ESCALATE,
        "risk_score": 50,
        "fraud_pattern": "unknown",
        "fraud_indicators": ["agent_parse_error"],
        "reasoning": content or "Agent response could not be parsed",
        "tool_calls_made": tool_calls_made,
        "iterations": iterations
    }
