# Code Walkthrough — Check Fraud Agent

A file-by-file walkthrough of every component in the system. Covers what each file does, how the code works, and how the pieces connect.

---

## How the System Flows

```
Check image uploaded to Blob Storage
        │
        ▼
  blob_trigger/__init__.py    ← OCR extraction via Document Intelligence
        │
        ▼ (Service Bus: tier1-checks-queue)
  tier1/__init__.py           ← Rule engine — scores the check
        │
        ├── risk_score ≤ 25 → APPROVE (done)
        ├── risk_score ≥ 75 → REJECT  (done)
        │
        ▼ (Service Bus: tier2-agent-queue)
  tier2/__init__.py           ← Dispatcher — reads TIER2_ENGINE
        │
        ├── TIER2_ENGINE=native → tier2/native.py   (raw ReAct loop)
        └── TIER2_ENGINE=sk    → tier2/sk_agent.py  (Semantic Kernel)
                │
                ├── approve / reject → done
                │
                ▼ (Service Bus: tier3-human-queue)
          tier3/__init__.py   ← Intake + HTTP analyst endpoints
                │
                ▼ (Dashboard)
          Analyst approves or rejects
```

---

## Entry Point — `function_app.py`

```python
app = df.DFApp(http_auth_level=func.AuthLevel.FUNCTION)

app.register_blueprint(blob_bp)
app.register_blueprint(tier1_bp)
app.register_blueprint(tier2_bp)
app.register_blueprint(tier3_bp)
```

This is the entire file — 14 lines. Azure Functions discovers triggers and routes by scanning the registered blueprints. Each tier defines its own `bp = df.Blueprint()` and this file just wires them together.

`DFApp` (Durable Functions App) is used instead of plain `func.FunctionApp` because it supports the Blueprint pattern for modular registration.

---

## Shared Utilities — `handlers/shared/`

### `cosmos.py` — Database Layer

Four functions that every tier uses:

```python
def get_cosmos_container(container_name: str)
```
Creates a Cosmos DB client from environment variables and returns a container handle. Called before every query — no persistent connection pooling (stateless functions).

```python
def get_customer(account_number: str)
```
Looks up a customer by account number using a **parameterized query** (not string interpolation — prevents injection). Returns the first match or `None`.

```python
def upsert_check(check: dict)
```
Writes or updates a check document. Automatically stamps `updated_at` before writing. Used by all three tiers to persist state changes.

```python
def write_audit_log(check_id, tier, decision, details)
```
Appends an immutable audit entry to the `audit_log` container. The `id` is `{check_id}-{tier}-{timestamp}` — unique per tier per check. This gives a full, searchable decision trail.

---

### `servicebus.py` — Messaging Layer

```python
def enqueue_message(queue_name: str, payload: dict)
```
Serialises the payload dict to JSON and sends it to a Service Bus queue. Used by Tier-1 (→ Tier-2) and Tier-2 (→ Tier-3). The queue name is passed in, not hardcoded — so the same function handles all three queues.

---

### `utils.py` — Rule Helpers

```python
def validate_micr(routing_number: str) -> bool
```
Validates a 9-digit ABA routing number using the official checksum algorithm:
```
(3×(d0+d3+d6) + 7×(d1+d4+d7) + (d2+d5+d8)) mod 10 == 0
```
Any routing number that fails this is flagged as `invalid_micr_checksum`.

```python
def confidence_gate(risk_score: int) -> str
```
Converts a numeric risk score into a decision. Thresholds come from environment variables:
```
risk_score ≤ 25  → "approve"
risk_score ≥ 75  → "reject"
26 – 74          → "escalate"
```
Named `TIER1_APPROVE_THRESHOLD` and `TIER1_REJECT_THRESHOLD` in config (note: the naming is inverted from what you might expect — 75 is the *reject* threshold).

```python
def get_account_age_days(opened_date: str) -> int
```
Parses an ISO 8601 date string and returns the number of days since the account was opened. Used in Tier-1 to flag new accounts submitting large checks.

---

### `velocity.py` — Shared Velocity Queries

```python
def query_velocity(account_number, days_back=1, exclude_check_id="") -> dict
```
Queries Cosmos for all checks from a given account within a time window. Returns:
```python
{
  "count":      int,    # number of checks in the window
  "total":      float,  # total dollar amount
  "near_ctr":   int,    # checks between $8,500–$9,999 (structuring indicator)
  "recent_txns": list   # raw transaction list
}
```

The `exclude_check_id` parameter prevents the current check from counting against its own velocity (used in Tier-1). Tier-2 omits this — the agent is doing a broader historical look.

The WHERE clause (`base_where`) is a hardcoded string. Only the values are parameterized — the f-string interpolation is safe here because it only injects the WHERE fragment, not user data.

Used by both `_t1_velocity` in Tier-1 and the `velocity_check` tool in Tier-2 — extracted here to eliminate duplication.

---

## Blob Trigger — `handlers/blob_trigger/__init__.py`

**Trigger:** Any file uploaded to the `check-images` container in Blob Storage.

### What it does

```python
@bp.blob_trigger(path="check-images/{name}", connection="BLOB_CONNECTION_STRING")
def blob_trigger(checkblob: func.InputStream):
```

1. **Size guard** — rejects files over 5MB before touching Document Intelligence (saves cost and prevents memory exhaustion)
2. **OCR extraction** — calls `_extract_check_fields()` which sends the image bytes to Azure Document Intelligence using the `prebuilt-check` model
3. **Payload assembly** — builds a structured check document with all extracted fields
4. **Enqueue** — sends the payload to the Tier-1 Service Bus queue

### `_extract_check_fields(blob_bytes)`

Calls the Document Intelligence `prebuilt-check` model which knows check-specific field layouts:

| Field extracted | Maps to |
|----------------|---------|
| `Amount` | `amount_numeric` |
| `AmountInWords` | `amount_words` |
| `PayToTheOrderOf` | `payee_name` |
| `AccountNumber` | `account_number` |
| `RoutingNumber` | `routing_number` |
| `MicrLine` | `micr_line` |
| `BankName` | `bank_name` |
| `DrawerName` | `customer_name` |
| `Signature` | `signature_present` (bool) |
| `Date` | `issue_date` |
| `doc.confidence` | `raw_ocr_confidence` |

After extraction, it runs two validation functions:
- `validate_micr(routing_number)` — checksum validation
- `_check_amount_mismatch(amount_numeric, amount_words)` — cross-checks numeric vs written amount (a 50%+ discrepancy flags alteration)

---

## Tier 1 — Rule Engine — `handlers/tier1/__init__.py`

**Trigger:** Service Bus message on `tier1-checks-queue`.

### Orchestration loop

```python
for check_fn in [_t1_micr, _t1_amount, _t1_account, _t1_velocity]:
    result = check_fn(payload)
    risk_score += result["risk_contribution"]
    fraud_indicators.extend(result["indicators"])
risk_score = min(risk_score, 100)
decision   = confidence_gate(risk_score)
```

All four rule functions run regardless of intermediate scores — no short-circuiting. This ensures the full indicator list is always captured. Scores accumulate and are capped at 100.

### The four rule functions

**`_t1_micr(payload)`** — Document integrity checks
Reads from `extracted_fields` (the OCR output). Checks: MICR checksum, amount mismatch, payee match, alteration, signature presence, OCR confidence.

**`_t1_amount(payload)`** — Amount flags
Three thresholds: near-CTR ($8,500–$9,999), large amount (>$50K), invalid (≤$0).

**`_t1_account(payload)`** — Account risk profile
Calls `get_customer()` — if not found, returns 60 points immediately (hard stop). Otherwise checks: account status, synthetic identity risk, KYC status, account age vs amount, and any custom flags on the account.

**`_t1_velocity(payload)`** — 24-hour transaction velocity
Calls `query_velocity()` with `days_back=1` and excludes the current check ID so the check doesn't count against itself. Flags high frequency (3+/5+ checks) and cumulative amounts approaching $9,000.

### After scoring

```python
payload.update({ "risk_score": ..., "fraud_decision": decision, "status": ... })
upsert_check(payload)
write_audit_log(check_id, "tier1", decision, {...})
if decision == "escalate":
    payload["tier1_risk_score"] = risk_score
    payload["tier1_indicators"] = fraud_indicators
    enqueue_message(os.environ["TIER2_QUEUE_NAME"], payload)
```

The Tier-1 risk score and indicators are attached to the payload **before** enqueueing to Tier-2, so the agent has full context on why the check was escalated.

---

## Tier 2 — Dispatcher — `handlers/tier2/__init__.py`

**Trigger:** Service Bus message on `tier2-agent-queue`.

```python
async def tier2_agent(msg: func.ServiceBusMessage):
    engine = os.environ.get("TIER2_ENGINE", "native")

    if engine == "sk":
        from handlers.tier2.sk_agent import run_agent_sk
        result = await run_agent_sk(payload, start)
    else:
        from handlers.tier2.native import run_agent_native
        result = run_agent_native(payload, start)
```

The trigger is `async` to support awaiting the SK engine. The native engine is sync but can be called from async context directly.

Both engines return the **same dict contract**:
```python
{
  "decision":        "approve" | "reject" | "escalate",
  "risk_score":      0–100,
  "fraud_pattern":   str | None,
  "fraud_indicators": list,
  "reasoning":       str,
  "tool_calls_made": list,
  "iterations":      int,
  "engine":          "native" | "semantic_kernel"
}
```

After either engine returns, the dispatcher writes to Cosmos and audit_log identically — neither engine needs to know about persistence. If the decision is `"escalate"`, the payload goes to the Tier-3 queue.

The `agent_engine` field is written to the Cosmos check document — visible in the dashboard and useful for comparing engine outputs side by side.

---

## Tier 2 — Native Engine — `handlers/tier2/native.py`

Implements the **ReAct (Reasoning + Acting)** pattern directly against the OpenAI API.

### TOOLS list

A manually crafted JSON schema list — one entry per tool. OpenAI reads these to know what functions the agent can call and what parameters they expect:

```python
TOOLS = [
    { "type": "function", "function": {
        "name": "customer_lookup",
        "description": "Look up full customer profile.",
        "parameters": { "type": "object", "properties": { "account_number": { "type": "string" } }, "required": ["account_number"] }
    }},
    ...  # velocity_check, signature_check, fraud_pattern_search, payee_verify, escalate_to_human
]
```

### SYSTEM_PROMPT

Sets the agent's role, rules, and required output format:
- Always start with `customer_lookup`
- Use `velocity_check` for near-CTR amounts
- Max 6 tool calls
- Only `escalate_to_human` after 3+ other tools
- Final response must be a JSON object in a specific shape

### `run_agent_native()` — The ReAct loop

```python
while iterations < MAX_ITERATIONS:
    if time.time() - start_time > TIMEOUT_SECONDS:
        break
    response = client.chat.completions.create(
        model=..., messages=messages, tools=TOOLS, tool_choice="auto", temperature=0.1
    )
    message = response.choices[0].message

    if not message.tool_calls:              # ← Agent finished — parse JSON decision
        return _parse_decision(message.content, ...)

    for tc in message.tool_calls:           # ← Agent wants to call tools
        result = _exec_tool(tc.function.name, args, payload)
        messages.append({"role": "tool", ...})  # ← Feed result back into conversation
```

Each iteration is one LLM call. If the model returns no tool calls, it has finished reasoning and produced its decision. If it returns tool calls, results are appended to the message history and the loop continues. The conversation history grows with each iteration — the agent always has full context of what it has already investigated.

### `_exec_tool()` — Tool dispatch

Routes tool calls to their implementations:

| Tool | Implementation |
|------|---------------|
| `customer_lookup` | `get_customer()` from shared cosmos |
| `velocity_check` | `query_velocity()` from shared velocity |
| `signature_check` | Reads `extracted_fields.signature_present` from payload |
| `fraud_pattern_search` | Queries `fraud_cases` Cosmos container, matches by signal overlap |
| `payee_verify` | Keyword check against known suspicious payee patterns |
| `escalate_to_human` | Records escalation args — loop exit handles the result |

### `_parse_decision()` — JSON extraction

The agent's final message is free text that contains a JSON object. This function finds the first `{` and last `}` and parses between them — tolerant of the model adding preamble text before the JSON.

---

## Tier 2 — Semantic Kernel Engine — `handlers/tier2/sk_agent.py`

Implements the same investigation as the native engine but structured as a **3-phase process** using Semantic Kernel.

### Phase 1 — `_context_step()` (Deterministic, no AI)

```python
def _context_step(payload: dict) -> dict:
    customer = get_customer(account_number)
    velocity = query_velocity(account_number, days_back=1, exclude_check_id=...)
    # Derive pre_signals list from the raw data
    return {"customer": customer, "velocity_1d": velocity, "pre_signals": pre_signals}
```

Runs **before** the AI agent starts. Customer data and 24h velocity are gathered deterministically and the key signals are pre-computed. This context is then passed to the agent in its initial message — the agent starts with more information than the native engine and can make more targeted tool calls.

### Phase 2 — `_investigation_step()` (Agentic, AI-driven)

**Kernel setup:**
```python
kernel = Kernel()
kernel.add_service(AzureChatCompletion(...))
kernel.add_plugin(FraudInvestigationPlugin(payload), plugin_name="fraud")
```

**Agent creation:**
```python
agent = ChatCompletionAgent(
    kernel=kernel,
    name="FraudInvestigationAgent",
    instructions=SYSTEM_PROMPT,
)
```

**Invocation:**
```python
async for response in agent.invoke(messages=history):
    for item in response.items:
        if isinstance(item, FunctionCallContent):
            tool_calls_made.append({"tool": item.function_name, "args": ...})
        elif hasattr(item, "text") and item.text:
            final_content = item.text
```

`agent.invoke()` is an async iterator. Each iteration is one agent step — the SK framework manages the tool call cycle internally. `FunctionCallContent` items are intercepted to track which tools were called and with what arguments.

### `FraudInvestigationPlugin` — Typed tool definitions

Instead of a hand-crafted TOOLS JSON list, tools are Python methods decorated with `@kernel_function`:

```python
class FraudInvestigationPlugin:

    @kernel_function(description="Look up full customer profile...")
    def customer_lookup(self, account_number: str) -> str:
        customer = get_customer(account_number)
        return json.dumps({...})

    @kernel_function(description="Check transaction frequency...")
    def velocity_check(self, account_number: str, days_back: int = 30) -> str:
        data = query_velocity(account_number, days_back=days_back)
        return json.dumps({...})
    
    # signature_check, fraud_pattern_search, payee_verify, escalate_to_human
```

SK reads the type annotations (`str`, `int`, `float`) and the `description` to auto-generate the JSON schema that gets sent to the model. No manual schema authoring. The plugin receives the original `payload` via its constructor so `signature_check` can access `extracted_fields` without needing it as an LLM parameter.

### Phase 3 — `_verdict_step()` (Deterministic)

```python
def _verdict_step(raw_content, tool_calls_made, iterations) -> dict:
    start = raw_content.find("{")
    end   = raw_content.rfind("}") + 1
    d = json.loads(raw_content[start:end])
    d.update({"tool_calls_made": ..., "iterations": ..., "engine": "semantic_kernel"})
    return d
```

Identical logic to `_parse_decision()` in the native engine — extracts the JSON decision from the agent's final text response and normalises it to the shared output contract.

### `run_agent_sk()` — Entry point

```python
async def run_agent_sk(payload, start_time) -> dict:
    context = _context_step(payload)              # Phase 1
    raw, calls, iters = await _investigation_step(payload, context, start_time)  # Phase 2
    return _verdict_step(raw, calls, iters)        # Phase 3
```

Three lines. Each phase is a named function with a clear responsibility — easy to log, test, or replace independently.

---

## Tier 3 — Human Review — `handlers/tier3/__init__.py`

Two responsibilities in one file: **Service Bus intake** and **HTTP analyst API**.

### `tier3_intake` — Service Bus trigger

```python
def tier3_intake(msg: func.ServiceBusMessage):
    payload["status"]            = "awaiting_analyst"
    payload["tier3_assigned_at"] = datetime.now(timezone.utc).isoformat()
    upsert_check(payload)
    write_audit_log(...)
```

Receives checks that Tier-2 escalated to human review. Simply stamps the check with `awaiting_analyst` status and a timestamp, then saves it. The check now appears in the analyst dashboard queue.

### `analyst_decision_endpoint` — POST `/api/checks/{check_id}/decision`

```python
decision = body.get("decision")
if decision not in ["approve", "reject"]:
    return func.HttpResponse("decision must be approve or reject", status_code=400)
```

Validates that the decision is one of two allowed values. Fetches the check from Cosmos, updates it with the analyst decision and metadata, saves it, and writes an audit log entry. Returns 200 with the recorded decision.

### `get_check_status` — GET `/api/checks/{check_id}/status`

Returns a lightweight status summary for a specific check — only `id`, `status`, `fraud_decision`, and `analyst_decision`. Used by the scenario runner's `--poll` flag to monitor progress.

### `get_analyst_queue` — GET `/api/checks/queue`

```python
query = "SELECT * FROM c WHERE c.status IN ('awaiting_analyst', 'escalated_tier3') ORDER BY c.tier3_assigned_at ASC"
```

Returns all checks currently waiting for analyst review, ordered by when they arrived. This is the queue that feeds the dashboard.

### `serve_dashboard` — GET `/api/dashboard`

Reads `dashboard.html` from the filesystem and returns it as `text/html`. The path is computed relative to the `tier3` directory (three levels up to the project root where `dashboard.html` lives).

---

## Key Design Decisions

### Blueprints over a single file
Each tier is a separate Python package with its own `bp = df.Blueprint()`. `function_app.py` just registers them. This keeps each tier independently readable and testable.

### Shared module for cross-cutting concerns
`handlers/shared/` holds everything that multiple tiers use — Cosmos queries, Service Bus, MICR validation, velocity checks. Nothing is duplicated across tiers.

### Parameterized Cosmos queries throughout
Every Cosmos query uses `parameters=[{"name": "@param", "value": value}]` — no f-string interpolation of user-controlled values into query strings.

### Same output contract from both Tier-2 engines
The dispatcher (`tier2/__init__.py`) knows nothing about what either engine does internally. Both engines return the same dict shape so the Cosmos write and audit log are identical regardless of which engine ran.

### Config-driven engine selection
`TIER2_ENGINE=native` or `sk` — changeable in Azure portal with no redeployment. The imports are lazy (inside the `if` block) so neither engine's dependencies are loaded unless that engine is selected.

### Audit trail on every decision
`write_audit_log()` is called at the end of every tier. The `audit_log` Cosmos container has one document per tier per check — giving a complete, immutable decision trail from blob upload to analyst resolution.
