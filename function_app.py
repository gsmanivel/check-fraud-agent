import os, json, uuid, time, logging
import azure.functions as func
import azure.durable_functions as df
from datetime import datetime, timezone, timedelta
from azure.cosmos import CosmosClient
from azure.servicebus import ServiceBusClient, ServiceBusMessage
from azure.ai.formrecognizer import DocumentAnalysisClient
from azure.core.credentials import AzureKeyCredential
from openai import AzureOpenAI

logger = logging.getLogger(__name__)
app = df.DFApp(http_auth_level=func.AuthLevel.FUNCTION)

APPROVE_THRESHOLD = int(os.environ.get("TIER1_APPROVE_THRESHOLD", "75"))
REJECT_THRESHOLD  = int(os.environ.get("TIER1_REJECT_THRESHOLD", "25"))
MAX_ITERATIONS    = int(os.environ.get("AGENT_MAX_ITERATIONS", "6"))
TIMEOUT_SECONDS   = int(os.environ.get("AGENT_TIMEOUT_SECONDS", "30"))

# ── Shared helpers ─────────────────────────────────────────────────────────────

def get_cosmos_container(container_name: str):
    client = CosmosClient(url=os.environ["COSMOS_ENDPOINT"], credential=os.environ["COSMOS_KEY"])
    db = client.get_database_client(os.environ["COSMOS_DATABASE"])
    return db.get_container_client(container_name)

def get_customer(account_number: str):
    container = get_cosmos_container(os.environ["COSMOS_CUSTOMERS_CONTAINER"])
    query = f"SELECT * FROM c WHERE c.account_number = '{account_number}'"
    items = list(container.query_items(query=query, enable_cross_partition_query=True))
    return items[0] if items else None

def upsert_check(check: dict):
    check["updated_at"] = datetime.now(timezone.utc).isoformat()
    container = get_cosmos_container(os.environ["COSMOS_CHECKS_CONTAINER"])
    container.upsert_item(check)

def write_audit_log(check_id: str, tier: str, decision: str, details: dict):
    container = get_cosmos_container(os.environ["COSMOS_AUDIT_CONTAINER"])
    log_entry = {
        "id": f"{check_id}-{tier}-{int(datetime.now(timezone.utc).timestamp())}",
        "check_id": check_id, "tier": tier, "decision": decision,
        "details": details, "timestamp": datetime.now(timezone.utc).isoformat()
    }
    container.create_item(log_entry)

def enqueue_message(queue_name: str, payload: dict):
    conn_str = os.environ["SERVICE_BUS_CONNECTION_STRING"]
    with ServiceBusClient.from_connection_string(conn_str) as client:
        with client.get_queue_sender(queue_name) as sender:
            sender.send_messages(ServiceBusMessage(json.dumps(payload)))

def validate_micr(routing_number: str) -> bool:
    if not routing_number or len(routing_number) != 9:
        return False
    try:
        d = [int(c) for c in routing_number]
        return (3*(d[0]+d[3]+d[6]) + 7*(d[1]+d[4]+d[7]) + (d[2]+d[5]+d[8])) % 10 == 0
    except Exception:
        return False

def confidence_gate(risk_score: int) -> str:
    if risk_score <= REJECT_THRESHOLD:   return "approve"
    elif risk_score >= APPROVE_THRESHOLD: return "reject"
    else:                                 return "escalate"

def get_account_age_days(opened_date: str) -> int:
    if not opened_date:
        return 0
    try:
        opened = datetime.fromisoformat(opened_date.replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - opened).days
    except Exception:
        return 0

# ══════════════════════════════════════════════════════════════════════════════
# BLOB TRIGGER
# ══════════════════════════════════════════════════════════════════════════════

@app.blob_trigger(
    arg_name="checkblob",
    path="check-images/{name}",
    connection="BLOB_CONNECTION_STRING"
)
def blob_trigger(checkblob: func.InputStream):
    blob_name = checkblob.name
    logger.info(f"Blob trigger fired: {blob_name}")
    try:
        blob_bytes = checkblob.read()
        extracted  = _extract_check_fields(blob_bytes)
        check_id   = str(uuid.uuid4())
        payload = {
            "id":               check_id,
            "check_number":     _parse_check_number(blob_name),
            "micr_line":        extracted.get("micr_line", ""),
            "account_number":   extracted.get("account_number", ""),
            "routing_number":   extracted.get("routing_number", ""),
            "customer_name":    extracted.get("customer_name", ""),
            "payee_name":       extracted.get("payee_name", ""),
            "amount":           extracted.get("amount_numeric", 0.0),
            "memo":             extracted.get("memo", ""),
            "issue_date":       extracted.get("issue_date", datetime.now(timezone.utc).isoformat()),
            "submission_date":  datetime.now(timezone.utc).isoformat(),
            "bank_name":        extracted.get("bank_name", ""),
            "check_image_url":  f"https://{os.environ.get('BLOB_ACCOUNT_NAME','storage')}.blob.core.windows.net/check-images/{blob_name}",
            "blob_path":        blob_name,
            "extracted_fields": extracted,
            "status":           "pending",
            "created_at":       datetime.now(timezone.utc).isoformat(),
            "updated_at":       datetime.now(timezone.utc).isoformat()
        }
        enqueue_message(os.environ["TIER1_QUEUE_NAME"], payload)
        logger.info(f"Check {check_id} enqueued to Tier 1")
    except Exception as e:
        logger.error(f"Blob trigger failed for {blob_name}: {e}", exc_info=True)
        raise

def _extract_check_fields(blob_bytes: bytes) -> dict:
    endpoint = os.environ["DOCUMENT_INTELLIGENCE_ENDPOINT"]
    key      = os.environ["DOCUMENT_INTELLIGENCE_KEY"]
    client   = DocumentAnalysisClient(endpoint=endpoint, credential=AzureKeyCredential(key))
    poller   = client.begin_analyze_document(model_id="prebuilt-check", document=blob_bytes)
    result   = poller.result()
    extracted = {
        "amount_numeric": 0.0, "amount_words": "", "payee_name": "",
        "payee_match": True, "micr_valid": True, "signature_present": False,
        "amount_mismatch": False, "alteration_detected": False,
        "raw_ocr_confidence": 0.0, "account_number": "", "routing_number": "",
        "micr_line": "", "bank_name": "", "memo": "", "issue_date": "", "customer_name": ""
    }
    if result.documents:
        doc    = result.documents[0]
        fields = doc.fields
        def fval(name):
            return fields[name].value if name in fields and fields[name].value else None
        extracted["amount_numeric"]     = float(fval("Amount") or 0)
        extracted["amount_words"]       = fval("AmountInWords") or ""
        extracted["payee_name"]         = fval("PayToTheOrderOf") or ""
        extracted["account_number"]     = fval("AccountNumber") or ""
        extracted["routing_number"]     = fval("RoutingNumber") or ""
        extracted["micr_line"]          = fval("MicrLine") or ""
        extracted["bank_name"]          = fval("BankName") or ""
        extracted["memo"]               = fval("Memo") or ""
        extracted["issue_date"]         = str(fval("Date") or "")
        extracted["customer_name"]      = fval("DrawerName") or ""
        extracted["signature_present"]  = fval("Signature") is not None
        extracted["raw_ocr_confidence"] = doc.confidence or 0.0
        extracted["micr_valid"]         = validate_micr(extracted["routing_number"])
        extracted["amount_mismatch"]    = _check_amount_mismatch(extracted["amount_numeric"], extracted["amount_words"])
    return extracted

def _check_amount_mismatch(amount_numeric: float, amount_words: str) -> bool:
    if not amount_words or amount_numeric == 0:
        return False
    try:
        first_word_digit = ''.join(filter(str.isdigit, amount_words.split()[0]))
        if first_word_digit:
            words_int   = int(first_word_digit)
            numeric_int = int(amount_numeric)
            if numeric_int > 0 and abs(numeric_int - words_int) / numeric_int > 0.5:
                return True
    except Exception:
        pass
    return False

def _parse_check_number(blob_name: str) -> str:
    try:
        parts = blob_name.split("_")
        if len(parts) >= 2:
            return parts[1]
    except Exception:
        pass
    return str(uuid.uuid4())[:8]

# ══════════════════════════════════════════════════════════════════════════════
# TIER 1 — rules engine
# ══════════════════════════════════════════════════════════════════════════════

@app.service_bus_queue_trigger(
    arg_name="msg",
    queue_name="%TIER1_QUEUE_NAME%",
    connection="SERVICE_BUS_CONNECTION_STRING"
)
def tier1_function(msg: func.ServiceBusMessage):
    start    = time.time()
    payload  = json.loads(msg.get_body().decode("utf-8"))
    check_id = payload.get("id")
    logger.info(f"Tier 1 processing: {check_id}")
    fraud_indicators = []
    checks_run       = {}
    risk_score       = 0
    try:
        for check_fn in [_t1_micr, _t1_amount, _t1_account, _t1_velocity]:
            result = check_fn(payload)
            checks_run[result["name"]] = result
            risk_score += result["risk_contribution"]
            fraud_indicators.extend(result["indicators"])
        risk_score = min(risk_score, 100)
        decision   = confidence_gate(risk_score)
        ms         = int((time.time() - start) * 1000)
        logger.info(f"Check {check_id} | Score:{risk_score} | {decision} | {ms}ms")
        payload.update({
            "risk_score":       risk_score,
            "fraud_indicators": fraud_indicators,
            "processing_tier":  "tier1",
            "fraud_decision":   decision,
            "status":           decision if decision != "escalate" else "escalated"
        })
        upsert_check(payload)
        write_audit_log(check_id, "tier1", decision, {"risk_score": risk_score, "ms": ms})
        if decision == "escalate":
            payload["tier1_risk_score"] = risk_score
            payload["tier1_indicators"] = fraud_indicators
            enqueue_message(os.environ["TIER2_QUEUE_NAME"], payload)
        msg.complete()
    except Exception as e:
        logger.error(f"Tier 1 failed {check_id}: {e}", exc_info=True)
        msg.abandon()
        raise

def _t1_micr(payload: dict) -> dict:
    indicators, risk = [], 0
    ef = payload.get("extracted_fields", {})
    if not ef.get("micr_valid", True):           indicators.append("invalid_micr_checksum");         risk += 40
    if ef.get("amount_mismatch", False):          indicators.append("amount_words_numeric_mismatch"); risk += 50
    if not ef.get("payee_match", True):           indicators.append("payee_name_mismatch");           risk += 35
    if ef.get("alteration_detected", False):      indicators.append("alteration_detected");           risk += 45
    if not ef.get("signature_present", True):     indicators.append("missing_signature");             risk += 20
    if ef.get("raw_ocr_confidence", 1.0) < 0.7:  indicators.append("low_ocr_confidence");            risk += 10
    return {"name": "micr_validation", "passed": risk == 0, "risk_contribution": risk, "indicators": indicators}

def _t1_amount(payload: dict) -> dict:
    indicators, risk = [], 0
    amount = payload.get("amount", 0)
    if 8500 <= amount <= 9999:  indicators.append("amount_near_ctr_threshold"); risk += 20
    if amount > 50000:          indicators.append("large_amount_check");        risk += 15
    if amount <= 0:             indicators.append("invalid_amount");            risk += 50
    return {"name": "amount_validation", "passed": risk == 0, "risk_contribution": risk, "indicators": indicators}

def _t1_account(payload: dict) -> dict:
    indicators, risk = [], 0
    customer = get_customer(payload.get("account_number", ""))
    if not customer:
        return {"name": "account_check", "passed": False, "risk_contribution": 60, "indicators": ["account_not_found"]}
    if customer.get("account_status") != "active":        indicators.append("account_not_active");      risk += 50
    if customer.get("synthetic_identity_risk", False):    indicators.append("synthetic_identity_flag"); risk += 40
    if not customer.get("kyc_verified", True):            indicators.append("kyc_not_verified");        risk += 30
    age = get_account_age_days(customer.get("opened_date", ""))
    if age < 90 and payload.get("amount", 0) > 5000:     indicators.append("new_account_large_amount");risk += 25
    for flag in customer.get("flags", []):                indicators.append(f"flag_{flag}");            risk += 20
    return {"name": "account_check", "passed": risk == 0, "risk_contribution": risk, "indicators": indicators}

def _t1_velocity(payload: dict) -> dict:
    indicators, risk = [], 0
    acct      = payload.get("account_number", "")
    container = get_cosmos_container(os.environ["COSMOS_CHECKS_CONTAINER"])
    cutoff    = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    count_q   = f"SELECT VALUE COUNT(1) FROM c WHERE c.account_number='{acct}' AND c.submission_date>='{cutoff}' AND c.id!='{payload.get('id','')}'"
    amount_q  = f"SELECT VALUE SUM(c.amount) FROM c WHERE c.account_number='{acct}' AND c.submission_date>='{cutoff}' AND c.id!='{payload.get('id','')}'"
    count_24h = (list(container.query_items(query=count_q,  enable_cross_partition_query=True)) or [0])[0]
    total_24h = ((list(container.query_items(query=amount_q, enable_cross_partition_query=True)) or [0])[0] or 0) + payload.get("amount", 0)
    if count_24h >= 5:    indicators.append("high_velocity_5plus_24h");      risk += 35
    elif count_24h >= 3:  indicators.append("elevated_velocity_3plus_24h");  risk += 15
    if total_24h >= 9000: indicators.append("cumulative_amount_near_ctr_24h"); risk += 30
    return {"name": "velocity_check", "passed": risk == 0, "risk_contribution": risk, "indicators": indicators, "count_24h": count_24h, "total_24h": total_24h}

# ══════════════════════════════════════════════════════════════════════════════
# TIER 2 — GPT-4o agent
# ══════════════════════════════════════════════════════════════════════════════

TOOLS = [
    {"type":"function","function":{"name":"customer_lookup","description":"Look up full customer profile.","parameters":{"type":"object","properties":{"account_number":{"type":"string"}},"required":["account_number"]}}},
    {"type":"function","function":{"name":"velocity_check","description":"Check transaction frequency and totals for structuring detection.","parameters":{"type":"object","properties":{"account_number":{"type":"string"},"days_back":{"type":"integer","default":30}},"required":["account_number"]}}},
    {"type":"function","function":{"name":"signature_check","description":"Validate check signature.","parameters":{"type":"object","properties":{"check_id":{"type":"string"},"account_number":{"type":"string"}},"required":["check_id","account_number"]}}},
    {"type":"function","function":{"name":"fraud_pattern_search","description":"Search fraud pattern knowledge base.","parameters":{"type":"object","properties":{"indicators":{"type":"array","items":{"type":"string"}},"account_number":{"type":"string"}},"required":["indicators"]}}},
    {"type":"function","function":{"name":"payee_verify","description":"Verify if payee is known or suspicious.","parameters":{"type":"object","properties":{"payee_name":{"type":"string"},"amount":{"type":"number"}},"required":["payee_name"]}}},
    {"type":"function","function":{"name":"escalate_to_human","description":"Escalate to human analyst. Use only after exhausting other tools.","parameters":{"type":"object","properties":{"reason":{"type":"string"},"suspected_pattern":{"type":"string","enum":["structuring","altered_check","synthetic_identity","unknown"]},"risk_score":{"type":"integer"}},"required":["reason","suspected_pattern","risk_score"]}}}
]

SYSTEM_PROMPT = """You are an expert check fraud detection agent.
Analyze escalated checks using available tools. Reason across ALL signals.
Rules:
- Start with customer_lookup
- Use velocity_check for amounts near $10,000
- Use fraud_pattern_search once you have indicators
- Maximum 6 tool calls total
- Only escalate_to_human after using at least 3 other tools
Always end with a JSON decision:
{"decision":"approve"|"reject"|"escalate","risk_score":0-100,"fraud_pattern":"structuring"|"altered_check"|"synthetic_identity"|"unknown"|null,"fraud_indicators":[],"reasoning":"explanation"}"""


@app.service_bus_queue_trigger(
    arg_name="msg",
    queue_name="%TIER2_QUEUE_NAME%",
    connection="SERVICE_BUS_CONNECTION_STRING"
)
def tier2_agent(msg: func.ServiceBusMessage):
    start    = time.time()
    payload  = json.loads(msg.get_body().decode("utf-8"))
    check_id = payload.get("id")
    logger.info(f"Tier 2 agent processing: {check_id}")
    try:
        result = _run_agent(payload, start)
        ms     = int((time.time() - start) * 1000)
        payload.update({
            "risk_score":       result["risk_score"],
            "fraud_decision":   result["decision"],
            "fraud_pattern":    result.get("fraud_pattern"),
            "fraud_indicators": result.get("fraud_indicators", []),
            "agent_reasoning":  result.get("reasoning", ""),
            "processing_tier":  "tier2",
            "status":           result["decision"] if result["decision"] != "escalate" else "escalated_tier3"
        })
        upsert_check(payload)
        write_audit_log(check_id, "tier2", result["decision"], {**result, "ms": ms})
        if result["decision"] == "escalate":
            enqueue_message(os.environ["TIER3_QUEUE_NAME"], payload)
        msg.complete()
        logger.info(f"Tier 2 done: {check_id} | {result['decision']} | {ms}ms")
    except Exception as e:
        logger.error(f"Tier 2 failed {check_id}: {e}", exc_info=True)
        msg.abandon()
        raise

def _run_agent(payload: dict, start_time: float) -> dict:
    client = AzureOpenAI(
        azure_endpoint=os.environ["AZURE_OPENAI_ENDPOINT"],
        api_key=os.environ["AZURE_OPENAI_KEY"],
        api_version="2024-08-01-preview"
    )
    ef = payload.get("extracted_fields", {})
    user_message = f"""Analyze this check for fraud:
CHECK: id={payload.get('id')} amount=${payload.get('amount',0):,.2f} payee={payload.get('payee_name')} account={payload.get('account_number')} bank={payload.get('bank_name')} submitted={payload.get('submission_date')}
EXTRACTION: micr_valid={ef.get('micr_valid')} amount_mismatch={ef.get('amount_mismatch')} signature={ef.get('signature_present')} ocr_confidence={ef.get('raw_ocr_confidence',1.0):.0%}
TIER1: risk_score={payload.get('tier1_risk_score','N/A')} indicators={', '.join(payload.get('tier1_indicators', [])) or 'None'}
Investigate and provide your final JSON decision."""

    messages        = [{"role":"system","content":SYSTEM_PROMPT},{"role":"user","content":user_message}]
    tool_calls_made = []
    iterations      = 0
    escalation      = None

    while iterations < MAX_ITERATIONS:
        if time.time() - start_time > TIMEOUT_SECONDS:
            break
        iterations += 1
        response = client.chat.completions.create(
            model=os.environ["AZURE_OPENAI_DEPLOYMENT"],
            messages=messages, tools=TOOLS, tool_choice="auto",
            temperature=0.1, max_tokens=2000
        )
        message = response.choices[0].message
        if not message.tool_calls:
            return _parse_decision(message.content, tool_calls_made, iterations)
        messages.append({"role":"assistant","content":message.content,"tool_calls":[
            {"id":tc.id,"type":"function","function":{"name":tc.function.name,"arguments":tc.function.arguments}}
            for tc in message.tool_calls
        ]})
        for tc in message.tool_calls:
            name   = tc.function.name
            args   = json.loads(tc.function.arguments)
            tool_calls_made.append({"tool":name,"args":args})
            result = _exec_tool(name, args, payload)
            if name == "escalate_to_human":
                escalation = args
            messages.append({"role":"tool","tool_call_id":tc.id,"content":json.dumps(result)})

    if escalation:
        return {"decision":"escalate","risk_score":escalation.get("risk_score",60),"fraud_pattern":escalation.get("suspected_pattern","unknown"),"fraud_indicators":[],"reasoning":escalation.get("reason",""),"tool_calls_made":tool_calls_made,"iterations":iterations}
    return {"decision":"escalate","risk_score":50,"fraud_pattern":"unknown","fraud_indicators":["max_iterations_reached"],"reasoning":"Agent reached iteration limit","tool_calls_made":tool_calls_made,"iterations":iterations}

def _exec_tool(name: str, args: dict, payload: dict) -> dict:
    try:
        if name == "customer_lookup":
            customer = get_customer(args["account_number"])
            if not customer:
                return {"found": False}
            return {"found":True,"customer_name":customer.get("customer_name"),"account_status":customer.get("account_status"),"kyc_verified":customer.get("kyc_verified"),"synthetic_identity_risk":customer.get("synthetic_identity_risk"),"avg_monthly_balance":customer.get("avg_monthly_balance"),"linked_accounts":customer.get("linked_accounts",[]),"flags":customer.get("flags",[])}
        elif name == "velocity_check":
            container = get_cosmos_container(os.environ["COSMOS_CHECKS_CONTAINER"])
            cutoff    = (datetime.now(timezone.utc) - timedelta(days=args.get("days_back",30))).isoformat()
            acct      = args["account_number"]
            count_q   = f"SELECT VALUE COUNT(1) FROM c WHERE c.account_number='{acct}' AND c.submission_date>='{cutoff}'"
            amount_q  = f"SELECT VALUE SUM(c.amount) FROM c WHERE c.account_number='{acct}' AND c.submission_date>='{cutoff}'"
            txn_q     = f"SELECT c.amount, c.submission_date FROM c WHERE c.account_number='{acct}' AND c.submission_date>='{cutoff}' ORDER BY c.submission_date DESC"
            count     = (list(container.query_items(query=count_q,  enable_cross_partition_query=True)) or [0])[0]
            total     = (list(container.query_items(query=amount_q, enable_cross_partition_query=True)) or [0])[0] or 0
            txns      = list(container.query_items(query=txn_q, enable_cross_partition_query=True))
            near_ctr  = sum(1 for t in txns if 8500 <= t.get("amount",0) <= 9999)
            return {"total_checks":count,"total_amount":round(total,2),"near_ctr_threshold_count":near_ctr,"structuring_risk":near_ctr>=2,"recent_transactions":txns[:10]}
        elif name == "signature_check":
            ef  = payload.get("extracted_fields", {})
            sig = ef.get("signature_present", False)
            return {"signature_present":sig,"signature_match":sig,"confidence":0.85 if sig else 0.0}
        elif name == "fraud_pattern_search":
            container  = get_cosmos_container("fraud_cases")
            all_cases  = list(container.read_all_items())
            indicators = set(args.get("indicators",[]))
            matches    = []
            for case in all_cases:
                overlap = set(case.get("agent_signals",[])).intersection(indicators)
                if overlap:
                    matches.append({"pattern":case.get("pattern"),"description":case.get("description"),"matching_signals":list(overlap),"match_confidence":len(overlap)/max(len(indicators),1)})
            matches.sort(key=lambda x: x["match_confidence"], reverse=True)
            return {"matches_found":len(matches),"top_matches":matches[:3]}
        elif name == "payee_verify":
            payee      = args.get("payee_name","")
            suspicious = any(kw in payee.lower() for kw in ["cash","bearer","atm","wire","anonymous"])
            return {"payee_name":payee,"suspicious":suspicious,"risk_assessment":"suspicious" if suspicious else "low_risk"}
        elif name == "escalate_to_human":
            return {"status":"escalation_queued","reason":args.get("reason")}
    except Exception as e:
        return {"error": str(e)}

def _parse_decision(content: str, tool_calls_made: list, iterations: int) -> dict:
    try:
        start = content.find("{")
        end   = content.rfind("}") + 1
        if start >= 0 and end > start:
            d = json.loads(content[start:end])
            d["tool_calls_made"] = tool_calls_made
            d["iterations"]      = iterations
            return d
    except Exception:
        pass
    return {"decision":"escalate","risk_score":50,"fraud_pattern":"unknown","fraud_indicators":["parse_error"],"reasoning":content or "Parse error","tool_calls_made":tool_calls_made,"iterations":iterations}

# ══════════════════════════════════════════════════════════════════════════════
# TIER 3 — Durable Function (human review)
# ══════════════════════════════════════════════════════════════════════════════

@app.service_bus_queue_trigger(
    arg_name="msg",
    queue_name="%TIER3_QUEUE_NAME%",
    connection="SERVICE_BUS_CONNECTION_STRING"
)
@app.durable_client_input(client_name="client")
async def tier3_starter(msg: func.ServiceBusMessage, client):
    payload     = json.loads(msg.get_body().decode("utf-8"))
    check_id    = payload.get("id")
    instance_id = await client.start_new("tier3_orchestrator", instance_id=check_id, client_input=payload)
    logger.info(f"Tier 3 orchestration started: {instance_id}")
    msg.complete()


@app.orchestration_trigger(context_name="context")
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
        yield context.call_activity("record_analyst_decision", {"payload": payload, "decision_data": {"decision":"timeout","analyst_id":"system","notes":"No response in 24h"}})


@app.activity_trigger(input_name="payload")
def notify_analyst(payload: dict):
    check_id = payload.get("id")
    payload["status"]            = "awaiting_analyst"
    payload["tier3_assigned_at"] = datetime.now(timezone.utc).isoformat()
    upsert_check(payload)
    write_audit_log(check_id, "tier3", "awaiting_analyst", {"risk_score": payload.get("risk_score"), "fraud_pattern": payload.get("fraud_pattern")})
    return {"status": "notified", "check_id": check_id}


@app.activity_trigger(input_name="data")
def record_analyst_decision(data: dict):
    payload       = data["payload"]
    decision_data = data["decision_data"]
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


@app.route(route="checks/{check_id}/decision", methods=["POST"])
@app.durable_client_input(client_name="client")
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
        json.dumps({"status":"recorded","check_id":check_id,"decision":body.get("decision")}),
        status_code=200, mimetype="application/json"
    )


@app.route(route="checks/{check_id}/status", methods=["GET"])
@app.durable_client_input(client_name="client")
async def get_check_status(req: func.HttpRequest, client) -> func.HttpResponse:
    check_id = req.route_params.get("check_id")
    status   = await client.get_status(check_id)
    if not status:
        return func.HttpResponse(json.dumps({"error":"Not found"}), status_code=404, mimetype="application/json")
    return func.HttpResponse(
        json.dumps({"check_id":check_id,"status":status.runtime_status.value}),
        status_code=200, mimetype="application/json"
    )


@app.route(route="checks/queue", methods=["GET"])
async def get_analyst_queue(req: func.HttpRequest) -> func.HttpResponse:
    container = get_cosmos_container(os.environ["COSMOS_CHECKS_CONTAINER"])
    query     = "SELECT c.id, c.check_number, c.amount, c.payee_name, c.account_number, c.risk_score, c.fraud_pattern, c.fraud_indicators, c.agent_reasoning, c.check_image_url, c.tier3_assigned_at FROM c WHERE c.status='awaiting_analyst' ORDER BY c.tier3_assigned_at ASC"
    items     = list(container.query_items(query=query, enable_cross_partition_query=True))
    return func.HttpResponse(
        json.dumps({"queue":items,"count":len(items)}),
        status_code=200, mimetype="application/json"
    )