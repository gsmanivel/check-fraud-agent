# Test Scenarios — Check Fraud Agent

Complete walkthrough and testing guide for all scenarios across both Tier-2 engines.

---

## Quick Reference

| Scenario | Description | Tier-1 Decision | Tier-2? | Tier-3? |
|----------|-------------|-----------------|---------|---------|
| A | Clean legitimate check | **approve** | No | No |
| B | Document fraud (altered check) | **reject** | No | No |
| C | Structuring — new account near CTR | escalate | **approve/reject** | Maybe |
| D | Synthetic identity | escalate | **reject/escalate** | Maybe |
| E | New account + oversized check | escalate | **investigate** | Maybe |
| F | Full chain — reaches human analyst | escalate | **escalate** | **Yes** |
| G | Engine comparison — same check, both engines | escalate | **both engines** | Maybe |

**Edge cases:** account-not-found, agent timeout, blob size limit, duplicate analyst decision.

---

## How to Run

```bash
# Run a specific scenario
python scripts/run_scenarios.py --scenario A

# Run with a specific engine (overrides local.settings.json)
python scripts/run_scenarios.py --scenario C --engine sk
python scripts/run_scenarios.py --scenario C --engine native

# Poll status after submitting
python scripts/run_scenarios.py --scenario D --poll

# Run all scenarios sequentially
python scripts/run_scenarios.py --all
```

After submitting, verify results:
- **Cosmos DB** — `checks` container, filter by the returned `check_id`
- **Dashboard** — `https://checkfraudagent.azurewebsites.net/api/dashboard`
- **Status API** — `GET /api/checks/{check_id}/status`

---

## Risk Scoring Reference

Tier-1 scores each check across four independent rule functions. Scores accumulate.

| Decision | Threshold |
|----------|-----------|
| **approve** | risk_score ≤ 25 |
| **escalate** | 26 – 74 |
| **reject** | risk_score ≥ 75 |

### MICR & Document Quality (`_t1_micr`)
| Signal | Points |
|--------|--------|
| Invalid MICR checksum | +40 |
| Amount words/numeric mismatch | +50 |
| Payee name mismatch | +35 |
| Alteration detected | +45 |
| Missing signature | +20 |
| OCR confidence < 70% | +10 |

### Amount Flags (`_t1_amount`)
| Signal | Points |
|--------|--------|
| Amount $8,500–$9,999 (near CTR) | +20 |
| Amount > $50,000 | +15 |
| Amount ≤ $0 | +50 |

### Account Flags (`_t1_account`)
| Signal | Points |
|--------|--------|
| Account not found | +60 (immediate) |
| Account not active | +50 |
| Synthetic identity risk | +40 |
| KYC not verified | +30 |
| Account < 90 days old AND amount > $5,000 | +25 |
| Each flag on account | +20 each |

### Velocity (`_t1_velocity`, 24-hour window)
| Signal | Points |
|--------|--------|
| 3–4 checks in 24h | +15 |
| 5+ checks in 24h | +35 |
| Cumulative 24h total ≥ $9,000 | +30 |

---

## Scenario A — Clean Legitimate Check

**Story:** A routine payment from a well-established account. Should sail through Tier-1 cleanly.

**Account:** `4645120421` — Steven Wilson
- Status: active | KYC: verified | Synthetic risk: No
- Opened: 2021-05-30 (1,810 days old) | Avg balance: $1,105

**Payload:**
```json
{
  "account_number": "4645120421",
  "routing_number": "122105155",
  "customer_name": "Steven Wilson",
  "payee_name": "Office Depot",
  "amount": 1500.00,
  "bank_name": "Capital One",
  "extracted_fields": {
    "micr_valid": true,
    "amount_mismatch": false,
    "payee_match": true,
    "alteration_detected": false,
    "signature_present": true,
    "raw_ocr_confidence": 0.98
  }
}
```

**Expected Tier-1 scoring:**
```
_t1_micr:     0   (all fields valid)
_t1_amount:   0   ($1,500 — no flags)
_t1_account:  0   (clean, established account)
_t1_velocity: 0   (no prior checks today)
─────────────────
Total:        0   → APPROVE
```

**What to verify in Cosmos:**
```json
{
  "status": "approve",
  "fraud_decision": "approve",
  "risk_score": 0,
  "processing_tier": "tier1"
}
```
Check does NOT appear in the analyst queue. No Tier-2 or Tier-3 activity.

---

## Scenario B — Document Fraud (Tier-1 Reject)

**Story:** A check with an altered amount and invalid MICR line — classic document fraud. Tier-1 should catch and reject immediately.

**Account:** `1478163327` — David Mitchell
- Status: active | KYC: verified | Synthetic risk: No
- Opened: 2022-01-23 (1,572 days old) | Avg balance: $2,557
- Note: The account itself is clean — the check document is the fraud vector.

**Payload:**
```json
{
  "account_number": "1478163327",
  "routing_number": "122105155",
  "customer_name": "David Mitchell",
  "payee_name": "Unknown Payee",
  "amount": 7500.00,
  "bank_name": "Chase",
  "extracted_fields": {
    "micr_valid": false,
    "amount_mismatch": true,
    "payee_match": true,
    "alteration_detected": false,
    "signature_present": false,
    "raw_ocr_confidence": 0.98
  }
}
```

**Expected Tier-1 scoring:**
```
_t1_micr:     90  (invalid_micr: +40, amount_mismatch: +50)
_t1_micr:     20  (missing_signature: +20)  → sub-total: 110 (capped at 100)
_t1_amount:    0  ($7,500 — no flags)
_t1_account:   0  (clean account)
_t1_velocity:  0
─────────────────
Total:        100  → REJECT
```

**What to verify in Cosmos:**
```json
{
  "status": "reject",
  "fraud_decision": "reject",
  "risk_score": 100,
  "fraud_indicators": ["invalid_micr_checksum", "amount_words_numeric_mismatch", "missing_signature"],
  "processing_tier": "tier1"
}
```
No Tier-2 activity. No audit analyst queue entry.

---

## Scenario C — Structuring Detection (Tier-1 → Tier-2)

**Story:** A new account submitting a check just below the $10,000 Currency Transaction Report (CTR) threshold. Classic structuring pattern. Tier-1 escalates; Tier-2 investigates with full tool suite.

**Account:** `7857221324` — Ashley Hill
- Status: active | KYC: verified | Synthetic risk: No
- Opened: **2026-04-01** (43 days old — new account)
- Avg balance: $13,481

**Payload:**
```json
{
  "account_number": "7857221324",
  "routing_number": "122105155",
  "customer_name": "Ashley Hill",
  "payee_name": "Global Ventures LLC",
  "amount": 9200.00,
  "bank_name": "Wells Fargo",
  "extracted_fields": {
    "micr_valid": true,
    "amount_mismatch": false,
    "payee_match": true,
    "alteration_detected": false,
    "signature_present": true,
    "raw_ocr_confidence": 0.98
  }
}
```

**Expected Tier-1 scoring:**
```
_t1_micr:      0  (clean document)
_t1_amount:   20  (near_ctr: $9,200 in $8,500–$9,999 range → +20)
_t1_account:  25  (new_account_large_amount: 43 days old + $9,200 > $5,000 → +25)
_t1_velocity:  0  (no prior checks today)
─────────────────
Total:        45  → ESCALATE to Tier-2
```

**Tier-2 — what the agent investigates:**
1. `customer_lookup` → new account (43 days), otherwise clean
2. `velocity_check` → checks 30-day history for structuring pattern
3. `fraud_pattern_search` → looks for near-CTR structuring patterns
4. `payee_verify` → checks if payee is suspicious
5. Final decision based on accumulated signals

**Engine comparison for this scenario:**
- **Native:** raw ReAct loop, tool calls driven by LLM in sequence
- **SK:** ContextStep pre-gathers customer + velocity data BEFORE AI starts → agent starts with richer initial context → typically uses fewer tool calls to reach same conclusion

**What to verify in Cosmos:**
```json
{
  "processing_tier": "tier2",
  "agent_engine": "native",   // or "semantic_kernel"
  "agent_tool_calls": [...],
  "agent_reasoning": "...",
  "fraud_indicators": ["amount_near_ctr_threshold", "new_account_large_amount"]
}
```

---

## Scenario D — Synthetic Identity Detection (Tier-1 → Tier-2)

**Story:** A flagged account with suspected synthetic identity and failed KYC. Tier-1 scores the account flags and escalates. Tier-2 does deep customer profiling.

**Account:** `9190197115` — Patricia Adams
- Status: active | KYC: **NOT verified** | Synthetic risk: **YES**
- Opened: 2026-01-27 (107 days old) | Avg balance: $3,463
- Flags: `["synthetic_identity_suspected"]`

**Payload:**
```json
{
  "account_number": "9190197115",
  "routing_number": "122105155",
  "customer_name": "Patricia Adams",
  "payee_name": "Sunrise Consulting",
  "amount": 3500.00,
  "bank_name": "Bank of America",
  "extracted_fields": {
    "micr_valid": true,
    "amount_mismatch": false,
    "payee_match": true,
    "alteration_detected": false,
    "signature_present": true,
    "raw_ocr_confidence": 0.98
  }
}
```

**Expected Tier-1 scoring:**
```
_t1_micr:      0  (clean document)
_t1_amount:    0  ($3,500 — no flags)
_t1_account:  70  (synthetic_identity_flag: +40, kyc_not_verified: +30)
_t1_velocity:  0
─────────────────
Total:        70  → ESCALATE to Tier-2  (70 < 75 threshold)
```

**Tier-2 — what the agent investigates:**
1. `customer_lookup` → reveals `synthetic_identity_risk: true`, `kyc_verified: false`, `flags: ["synthetic_identity_suspected"]`
2. `fraud_pattern_search` → searches for synthetic identity patterns
3. Based on profile: likely **reject** or **escalate to human**

**What to verify:**
- `fraud_indicators` should include `synthetic_identity_flag`, `kyc_not_verified`
- `fraud_pattern` should be `synthetic_identity`
- Check `agent_tool_calls` to see what the agent investigated

---

## Scenario E — New Account + Oversized Check (Tier-1 → Tier-2)

**Story:** A very recently opened account (76 days) receiving a $12,000 check — well above typical balance. Tier-1 flags the risk profile; Tier-2 digs into the account history.

**Account:** `7803990970` — Linda Allen
- Status: active | KYC: verified | Synthetic risk: No
- Opened: **2026-02-27** (76 days old — new account)
- Avg balance: $34,085

**Payload:**
```json
{
  "account_number": "7803990970",
  "routing_number": "122105155",
  "customer_name": "Linda Allen",
  "payee_name": "Apex Holdings",
  "amount": 12000.00,
  "bank_name": "US Bank",
  "extracted_fields": {
    "micr_valid": true,
    "amount_mismatch": false,
    "payee_match": true,
    "alteration_detected": false,
    "signature_present": true,
    "raw_ocr_confidence": 0.98
  }
}
```

**Expected Tier-1 scoring:**
```
_t1_micr:      0  (clean document)
_t1_amount:   15  (large_amount: $12,000 > $10,000 → +15)
_t1_account:  25  (new_account_large_amount: 76 days + $12,000 > $5,000 → +25)
_t1_velocity:  0
─────────────────
Total:        40  → ESCALATE to Tier-2
```

**Tier-2 — what the agent investigates:**
1. `customer_lookup` → new account, high balance, otherwise clean
2. `velocity_check` → checks recent check history for this account
3. `payee_verify` → assesses payee
4. Decision depends on velocity history and payee risk

**Note:** Account 7803990970 is part of the structuring fraud cluster in the synthetic dataset (along with accounts 3553440342 and 6320115677). If Cosmos is seeded, velocity_check may surface multiple recent checks from this account — elevating the risk significantly.

---

## Scenario F — Full Chain to Human Analyst (Tier-1 → Tier-2 → Tier-3)

**Story:** A check from a synthetic identity account that passes Tier-1 narrowly and is complex enough that the AI agent escalates to a human analyst. This exercises the entire 3-tier pipeline.

**Account:** `6237376063` — Anthony Perez
- Status: active | KYC: **NOT verified** | Synthetic risk: **YES**
- Opened: 2025-11-01 (194 days old) | Avg balance: $49,055
- Flags: `["synthetic_identity_suspected"]`

**Payload:**
```json
{
  "account_number": "6237376063",
  "routing_number": "122105155",
  "customer_name": "Anthony Perez",
  "payee_name": "Summit Capital Partners",
  "amount": 2500.00,
  "bank_name": "Chase",
  "extracted_fields": {
    "micr_valid": true,
    "amount_mismatch": false,
    "payee_match": true,
    "alteration_detected": false,
    "signature_present": true,
    "raw_ocr_confidence": 0.98
  }
}
```

**Expected Tier-1 scoring:**
```
_t1_micr:      0
_t1_amount:    0  ($2,500 — below all thresholds)
_t1_account:  70  (synthetic_identity_flag: +40, kyc_not_verified: +30)
_t1_velocity:  0
─────────────────
Total:        70  → ESCALATE to Tier-2
```

**Tier-2 — expected escalation path:**
1. `customer_lookup` → synth identity, unverified KYC, high balance ($49K) — suspicious profile
2. `fraud_pattern_search` → synthetic identity pattern match
3. `signature_check` → passes
4. Complexity + regulatory sensitivity → agent calls `escalate_to_human`
5. Check lands in Tier-3 queue with `status: awaiting_analyst`

**Tier-3 — analyst actions:**
```bash
# View queue
GET /api/checks/queue

# Approve
POST /api/checks/{check_id}/decision
{"decision": "approve", "analyst_id": "analyst01", "notes": "Verified identity documents in person"}

# Reject
POST /api/checks/{check_id}/decision
{"decision": "reject", "analyst_id": "analyst01", "notes": "Synthetic identity confirmed — flagging for SAR"}
```

**What to verify end-to-end:**
```json
{
  "status": "analyst_approve",       // or "analyst_reject"
  "fraud_decision": "escalate",      // tier2 decision
  "analyst_decision": "reject",      // human decision
  "analyst_id": "analyst01",
  "processing_tier": "tier2",
  "tier3_assigned_at": "...",
  "tier3_completed_at": "..."
}
```

**Audit trail:** Check `audit_log` container for three entries — `tier1`, `tier2`, `tier3` — showing the full chain.

> **Note:** The AI agent's decision is non-deterministic. If Tier-2 rejects directly (rather than escalating), it demonstrates the agent identified the fraud with high confidence. Either outcome is valid for the demo — the story is the AI's reasoning visible in `agent_reasoning` and `agent_tool_calls`.

---

## Scenario G — Engine Comparison (Same Check, Both Engines)

**Story:** Submit the identical payload twice — once with `TIER2_ENGINE=native` and once with `TIER2_ENGINE=sk`. Compare the reasoning paths, tool call sequences, and final decisions side by side.

**Account:** `1478163327` — David Mitchell (clean account — making the fraud signals subtle)

**Payload:**
```json
{
  "account_number": "1478163327",
  "routing_number": "122105155",
  "customer_name": "David Mitchell",
  "payee_name": "Harbor Services Inc",
  "amount": 8700.00,
  "bank_name": "Chase",
  "extracted_fields": {
    "micr_valid": true,
    "amount_mismatch": false,
    "payee_match": true,
    "alteration_detected": false,
    "signature_present": false,
    "raw_ocr_confidence": 0.98
  }
}
```

**Expected Tier-1 scoring:**
```
_t1_micr:     20  (missing_signature: +20)
_t1_amount:   20  (near_ctr: $8,700 in $8,500–$9,999 range → +20)
_t1_account:   0  (clean, 4-year-old account)
_t1_velocity:  0
─────────────────
Total:        40  → ESCALATE to Tier-2
```

**Run procedure:**
```bash
# Step 1: Run with native engine
python scripts/run_scenarios.py --scenario G --engine native
# Note the check_id → call it check_native

# Step 2: Run with SK engine
python scripts/run_scenarios.py --scenario G --engine sk
# Note the check_id → call it check_sk

# Step 3: Compare outputs
GET /api/checks/{check_native}/status
GET /api/checks/{check_sk}/status
```

**What to compare:**

| Field | Native engine | SK engine |
|-------|--------------|-----------|
| `agent_engine` | `"native"` | `"semantic_kernel"` |
| `agent_tool_calls` | Sequential ReAct loop | 3-phase: context pre-gathered, then agent |
| `agent_iterations` | Number of LLM calls | Number of agent.invoke iterations |
| `fraud_indicators` | AI-derived | AI-derived (may differ) |
| `agent_reasoning` | Raw LLM output | SK-managed output |
| `risk_score` | AI-assigned | AI-assigned (may differ slightly) |
| Processing time (ms) | Baseline | ~1-2s longer (Kernel init overhead) |

**Key talking point for the demo:**
> "Both engines use the same underlying GPT-4o model and the same 5 tools. The difference is the framework. The native engine is a raw OpenAI SDK while loop — you can see every step. The Semantic Kernel engine structures the investigation as three explicit process phases: the ContextStep deterministically gathers customer and velocity data BEFORE the AI agent starts. This means the agent begins with richer context and typically makes more targeted tool calls."

---

## Edge Cases

### Edge Case 1 — Account Not Found

**Purpose:** Verify the account-not-found path gives maximum risk.

```json
{
  "account_number": "9999999999",
  "amount": 500.00,
  "payee_name": "Test Payee",
  "bank_name": "Test Bank",
  "extracted_fields": {
    "micr_valid": true, "amount_mismatch": false,
    "payee_match": true, "alteration_detected": false,
    "signature_present": true, "raw_ocr_confidence": 0.98
  }
}
```

**Expected:** `_t1_account` returns `account_not_found` (+60) immediately. Total score 60 → escalate to Tier-2.
Tier-2 `customer_lookup` returns `{"found": false}` — agent should escalate or reject.

---

### Edge Case 2 — Agent Timeout

**Purpose:** Verify graceful timeout handling in Tier-2.

**Setup:** Temporarily set `AGENT_TIMEOUT_SECONDS=1` in Azure Function App Settings (or local.settings.json).
Submit any Scenario C/D/E payload. The agent will hit the timeout before completing its investigation.

**Expected Cosmos result:**
```json
{
  "fraud_decision": "escalate",
  "fraud_indicators": ["max_iterations_reached"],
  "agent_reasoning": "Agent reached iteration limit"
}
```

**Restore:** Set `AGENT_TIMEOUT_SECONDS=30` after testing.

---

### Edge Case 3 — Blob Size Limit

**Purpose:** Verify oversized files are rejected before hitting Document Intelligence.

**Setup:** Upload a file > 5MB to the `check-images` container in Azure Blob Storage.

**Expected:** Function App logs show:
```
Blob {name} exceeds 5MB limit ({size} bytes); skipping
```
No Cosmos record created. No Service Bus message sent.

---

### Edge Case 4 — Invalid Analyst Decision Body

**Purpose:** Verify the decision endpoint validates input.

```bash
# Invalid decision value
curl -X POST https://checkfraudagent.azurewebsites.net/api/checks/{check_id}/decision \
  -H "Content-Type: application/json" \
  -d '{"decision": "maybe"}'
# Expected: 400 — "decision must be approve or reject"

# Malformed JSON
curl -X POST .../decision \
  -H "Content-Type: application/json" \
  -d 'not json'
# Expected: 400 — "Invalid JSON"

# Check not found
curl -X POST .../decision \
  -H "Content-Type: application/json" \
  -d '{"decision": "approve"}' \
  # (using a non-existent check_id)
# Expected: 404 — {"error": "Check not found"}
```

---

### Edge Case 5 — Duplicate Analyst Decision

**Purpose:** Verify behavior when the same check is decided twice.

Submit a decision → then submit a second decision on the same check.

**Current behavior:** The second decision overwrites the first (no idempotency guard — known gap).
Both decisions are recorded in `audit_log` so the audit trail is preserved.

**Expected:** Second call returns 200. Cosmos shows the latest decision. Two entries in audit_log.

---

## Dashboard Verification Checklist

After running scenarios, confirm the dashboard at `/api/dashboard` shows:

- [ ] **Queue tab** — Scenarios D, E, F visible under `awaiting_analyst` status
- [ ] **Check details** — `fraud_indicators`, `agent_reasoning`, `agent_tool_calls` visible
- [ ] **Engine field** — `agent_engine: native` or `agent_engine: semantic_kernel` shown on each card
- [ ] **Approve/Reject buttons** — functional for `awaiting_analyst` checks
- [ ] **After decision** — status updates to `analyst_approve` or `analyst_reject`; check disappears from queue

---

## Demo Script (5-minute flow)

1. **"A check just arrived"** → open dashboard, show queue is empty
2. **Submit Scenario B** (obvious fraud) → "Tier-1 catches it in milliseconds — no AI needed"
   - Show Cosmos: `risk_score: 100, fraud_decision: reject`
3. **Submit Scenario C** (structuring) with `TIER2_ENGINE=native`
   - "Tier-1 isn't sure — it escalates to our AI agent"
   - Show Cosmos: `agent_tool_calls`, `agent_reasoning`
   - "The native engine is a raw OpenAI ReAct loop — every tool call is visible"
4. **Flip `TIER2_ENGINE=sk` in Azure portal** (30 seconds — no redeployment)
5. **Submit Scenario G** with SK engine
   - "Same check, same model — now running on Semantic Kernel, Microsoft's enterprise agentic AI SDK"
   - "The SK engine pre-gathers evidence deterministically before the AI starts — Phase 1 is always auditable"
   - Compare the two Cosmos documents side by side
6. **Submit Scenario F** → check appears in dashboard queue
   - "The AI decided this needs a human — responsible AI in action"
   - Click Approve or Reject on dashboard
   - "End-to-end audit trail from Blob Storage through three tiers to human decision"

---

## Test Accounts Summary

| Account | Customer | Age (days) | KYC | Synth | Use in |
|---------|----------|-----------|-----|-------|--------|
| `4645120421` | Steven Wilson | 1,810 | ✓ | No | Scenario A |
| `1478163327` | David Mitchell | 1,572 | ✓ | No | Scenarios B, G |
| `7857221324` | Ashley Hill | 43 | ✓ | No | Scenario C |
| `9190197115` | Patricia Adams | 107 | ✗ | **Yes** | Scenario D |
| `7803990970` | Linda Allen | 76 | ✓ | No | Scenario E |
| `6237376063` | Anthony Perez | 194 | ✗ | **Yes** | Scenario F |
| `9999999999` | (unknown) | — | — | — | Edge Case 1 |
