# Check Fraud Detection Agent

AI-powered check fraud detection on Azure — a 3-tier pipeline that automates the high-confidence decisions, uses an AI agent to investigate the ambiguous middle, and only routes the genuinely uncertain cases to human analysts.

Built with **Azure Functions (Python v2)**, **Azure Document Intelligence**, **Azure OpenAI (GPT-4o)**, **Cosmos DB**, and **Service Bus**.

---

## The Business Problem

Banks process thousands of checks daily — mobile deposits, branch scans, ATM captures. Reviewing each one manually for fraud (forged signatures, altered amounts, money laundering, synthetic identities) is slow, expensive, and inconsistent. Compliance regulations (BSA/AML, CTR reporting, KYC) require a defensible decision trail on every check.

This system:
- **Auto-decides 80–90% of checks** within milliseconds via deterministic rules
- **Investigates ambiguous cases** with an AI agent that reasons across customer history, velocity, fraud knowledge base, and check metadata
- **Escalates only the truly uncertain** to a human analyst dashboard
- **Writes an immutable audit trail** for every decision — regulator-ready

---

## How It Works — End-to-End Flow

```
Check image upload
    → Blob Storage
        → Blob Trigger Function          ← Document Intelligence OCR
            → Service Bus (tier1-queue)
                → Tier 1 Function        ← Deterministic rule engine (~200ms)
                    ├─ Score ≤ 25 → AUTO APPROVE
                    ├─ Score ≥ 75 → AUTO REJECT
                    └─ 26–74 → Service Bus (tier2-queue)
                        → Tier 2 Agent   ← GPT-4o ReAct loop, dual engine
                            ├─ approve / reject
                            └─ escalate → Service Bus (tier3-queue)
                                → Tier 3 Function → Analyst Dashboard
                                    → POST /checks/{id}/decision
```

Full architecture diagram: [docs/architecture.md](docs/architecture.md)

---

## The Three Tiers

### Tier 1 — Rule Engine ([handlers/tier1/__init__.py](handlers/tier1/__init__.py))

A fast, deterministic, **explainable** scorer that runs four checks and produces a 0–100 risk score:

| Check | Business concern | Examples |
|---|---|---|
| **MICR validation** | Forgery / alteration | Routing checksum invalid, amount in words ≠ numeric, missing signature, alteration detected, low OCR confidence |
| **Amount validation** | Money laundering | Amount in $8,500–$9,999 (just under the $10K CTR reporting threshold), unusually large amount, zero/negative |
| **Account check** | KYC / identity fraud | Closed account, synthetic identity flag, KYC not verified, new account (<90 days) with large amount, customer flags |
| **Velocity check** | Structuring / kiting | 3+ or 5+ checks in 24h, cumulative 24h amount near $9,000 |

**Decision gate:** score ≤ 25 approve · score ≥ 75 reject · 26–74 escalate to Tier 2.

### Tier 2 — AI Investigation Agent ([handlers/tier2/](handlers/tier2/))

For the ambiguous middle, a GPT-4o agent acts like a junior fraud analyst. It runs a ReAct loop (max 6 iterations, 30s timeout) and chooses from these tools:

| Tool | Purpose |
|---|---|
| `customer_lookup` | Account status, KYC, balance, linked accounts, flags |
| `velocity_check` | 30-day transaction pattern — detects **structuring** |
| `signature_check` | Validates check signature |
| `fraud_pattern_search` | Matches indicators against the `fraud_cases` knowledge base |
| `payee_verify` | Flags suspicious payees ("CASH", "BEARER", "ATM", anonymous wires) |
| `escalate_to_human` | Allowed only after at least 3 other tools have been called |

The agent emits a structured JSON decision: `approve` / `reject` / `escalate` with `risk_score`, `fraud_pattern`, `fraud_indicators`, and a human-readable `reasoning` field.

**Dual engine** — pick at runtime via `TIER2_ENGINE` env var:
- `native` ([handlers/tier2/native.py](handlers/tier2/native.py)) — hand-rolled ReAct loop directly on the Azure OpenAI SDK
- `sk` ([handlers/tier2/sk_agent.py](handlers/tier2/sk_agent.py)) — Microsoft Semantic Kernel 3-phase process

Both engines return the same output contract, making A/B benchmarking trivial.

### Tier 3 — Human Analyst Review ([handlers/tier3/__init__.py](handlers/tier3/__init__.py))

Only the truly uncertain cases reach a human. The analyst dashboard ([dashboard.html](dashboard.html)) polls the queue and shows the full decision context for each check:

- Tier 1 score breakdown and indicators
- Tier 2 agent reasoning text
- The tools the agent invoked and what they returned
- The original check image
- All fraud indicators split by tier

The analyst clicks Approve or Reject. The decision is logged with the analyst ID and any notes via `POST /api/checks/{id}/decision`.

---

## Fraud Patterns Detected

| Pattern | Detection tier | Key signals |
|---|---|---|
| Altered check | Tier 1 | MICR mismatch, amount words vs numeric, alteration flag |
| Forgery / bad signature | Tier 1 | Missing signature, payee mismatch |
| Money laundering — CTR evasion | Tier 1 | Single amount $8,500–$9,999, cumulative 24h ≥ $9,000 |
| Structuring | Tier 2 | Multiple sub-$10k checks across 30 days |
| Synthetic identity | Tier 2 → Tier 3 | KYC gaps, linked accounts, bust-out pattern |
| New-account abuse | Tier 2 | Account < 90 days old + large amount |
| Suspicious payee | Tier 2 | Payee = CASH / BEARER / ATM / WIRE |

---

## Compliance & Auditability

Every tier decision writes to an immutable **audit log** via `write_audit_log()` in [handlers/shared/cosmos.py](handlers/shared/cosmos.py). For any check, auditors can reconstruct: who/what made each decision, when, on what evidence, and with what risk score. This is required for SOX, BSA/AML, and CTR reporting.

---

## Project Structure

```
handlers/
  blob_trigger/   ← OCR extraction via Document Intelligence, enqueues to Tier-1
  tier1/          ← Rule engine, scores checks, escalates/approves/rejects
  tier2/          ← AI agent dispatcher, native.py, sk_agent.py
  tier3/          ← Analyst intake, HTTP endpoints, dashboard
  shared/         ← cosmos.py, servicebus.py, velocity.py, utils.py
scripts/          ← Manual dev tools (never deployed)
tests/            ← Automated pytest suite (runs in CI)
docs/             ← Architecture, tech stack, scenarios, code walkthrough
```

---

## Data Model (Cosmos DB)

Database `CheckFraudDB` with four containers:

| Container | Partition key | Purpose |
|---|---|---|
| `checks` | `/id` | Every check, with full lifecycle status |
| `customers` | `/account_number` | Account holder profiles (KYC, balance, flags) |
| `audit_log` | `/check_id` | Immutable per-tier decision trail |
| `fraud_cases` | `/id` | Curated fraud-pattern knowledge base for Tier 2 |

Detailed schema: [docs/architecture.md](docs/architecture.md#cosmos-db-schema).

Seed with synthetic data:
```bash
python scripts/seed_cosmos.py
```

---

## Azure Resources

| Resource | SKU / Plan | Purpose |
|---|---|---|
| Function App (Python 3.11) | Flex Consumption | Hosts all functions |
| Storage Account | Standard LRS | Blob storage for check images |
| Service Bus | Standard | 3 queues: tier1, tier2, tier3 |
| Cosmos DB (NoSQL) | Serverless | checks, customers, audit_log, fraud_cases |
| Document Intelligence | S0 | Check field extraction (`prebuilt-check` model) |
| Azure OpenAI | gpt-4o deployment | Tier 2 agent |
| Application Insights | — | Distributed tracing |
| Key Vault | — | Secrets management |

---

## API Endpoints

| Method | Route | Description |
|---|---|---|
| GET | `/api/dashboard` | Tier 3 analyst review UI |
| GET | `/api/checks/queue` | Returns checks awaiting analyst review |
| GET | `/api/checks/{id}/status` | Get processing status of a specific check |
| POST | `/api/checks/{id}/decision` | Analyst submits approve / reject |

---

## Key Environment Variables

| Variable | Purpose |
|---|---|
| `TIER2_ENGINE` | `native`, `sk`, or `azure_agent` — selects the Tier-2 agent engine |
| `AGENT_TIMEOUT_SECONDS` | Max seconds for Tier-2 agent (default 30) |
| `AGENT_MAX_ITERATIONS` | Max ReAct loop iterations (default 6, applies to `native`/`sk`) |
| `AZURE_AI_AGENTS_ENDPOINT` | Foundry endpoint for `azure_agent` engine — falls back to `AZURE_OPENAI_ENDPOINT` if unset. Requires Entra ID auth (`DefaultAzureCredential`); `az login` locally or system-assigned MI in Azure with the **Azure AI User** role. |
| `COSMOS_CHECKS_CONTAINER` | Cosmos container name for checks |
| `COSMOS_CUSTOMERS_CONTAINER` | Cosmos container name for customers |
| `SERVICE_BUS_CONNECTION_STRING` | Service Bus connection |
| `TIER1_QUEUE_NAME` / `TIER2_QUEUE_NAME` / `TIER3_QUEUE_NAME` | Queue names per tier |
| `AZURE_OPENAI_ENDPOINT` / `AZURE_OPENAI_KEY` / `AZURE_OPENAI_DEPLOYMENT` | Azure OpenAI config |
| `DOCUMENT_INTELLIGENCE_ENDPOINT` / `DOCUMENT_INTELLIGENCE_KEY` | Doc Intelligence config |

---

## Getting Started — GitHub Codespaces

1. Fork this repo on GitHub
2. Click **Code → Codespaces → New codespace**
3. Wait ~2 minutes for the container to build
4. Create `local.settings.json` at the repo root and fill in your Azure credentials — this file is gitignored, see [Key Environment Variables](#key-environment-variables) for required keys
5. Run locally: `func start`

---

## Deployment

CI/CD via GitHub Actions on push to `main`:

1. Run `pytest tests/`
2. Azure login via OIDC (federated credential on service principal `check-fraud-agent-github` — no stored secrets)
3. Zip `host.json requirements.txt function_app.py handlers/ dashboard.html`
4. Deploy via `az functionapp deployment source config-zip`

**Required GitHub secrets:**
- `AZURE_CLIENT_ID`, `AZURE_TENANT_ID`, `AZURE_SUBSCRIPTION_ID` (for OIDC federated login)

---

## Deployed URLs

| Resource | URL |
|---|---|
| Function App | `https://checkfraudagent.azurewebsites.net` |
| Analyst Dashboard | `https://checkfraudagent.azurewebsites.net/api/dashboard` |

> Note: Flex Consumption plan — the URL is **not** `checkfraudagent.azurewebsites.net`.

---

## Running Tests

```bash
pytest tests/ -v
```

---

## Demo / Test Scenarios

Run with `python scripts/run_scenarios.py --scenario X --engine native/sk --poll`:

| Scenario | Account | Expected path |
|---|---|---|
| A | 4645120421 (Steven Wilson) | Tier-1 APPROVE — clean check |
| B | 1478163327 (David Mitchell) | Tier-1 REJECT — altered / forgery |
| C | 7857221324 (Ashley Hill) | Tier-1 → Tier-2 — structuring |
| D | 9190197115 (Patricia Adams) | Tier-1 → Tier-2 — synthetic identity |
| E | 7803990970 (Linda Allen) | Tier-1 → Tier-2 — new account + large amount |
| F | 6237376063 (Anthony Perez) | Tier-1 → Tier-2 → Tier-3 — analyst review |
| G | 1478163327 (David Mitchell) | Engine comparison (run both `native` and `sk`) |
| X1 | 9999999999 | Edge case — account not found |

---

## Business Outcomes

1. **Speed** — low- and high-risk checks decided in milliseconds, no human in the loop
2. **Cost reduction** — only the ambiguous 26–74 zone consumes AI/analyst time
3. **Explainability** — every decision carries a risk score, named indicators, and (for Tier 2) the agent's reasoning + tool trace
4. **Adaptability** — new fraud patterns are added to `fraud_cases` and picked up by Tier 2 with no code change
5. **Compliance-ready** — built-in CTR-threshold detection, KYC enforcement, synthetic identity flagging, and a tamper-evident audit log

---

## Further Reading

- [docs/architecture.md](docs/architecture.md) — Full architecture, Mermaid diagram, Cosmos schema
- [docs/tech_stack.md](docs/tech_stack.md) — Technology choices and rationale
- [docs/code_walkthrough.md](docs/code_walkthrough.md) — File-by-file code tour
- [docs/test_scenarios.md](docs/test_scenarios.md) — Detailed scenario expectations
- [CLAUDE.md](CLAUDE.md) — Code conventions and contribution guidelines
