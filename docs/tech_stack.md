# Tech Stack — Check Fraud Agent

## Language & Runtime

| Component | Technology | Version |
|-----------|-----------|---------|
| Language | Python | 3.11 |
| Runtime | Azure Functions | v2 (Python worker) |
| Hosting model | Flex Consumption Plan | Serverless, scale-to-zero |

Flex Consumption gives per-execution billing with no idle cost — ideal for an event-driven fraud pipeline where volume is unpredictable.

---

## Azure Services

| Service | Purpose in this system |
|---------|------------------------|
| **Azure Functions** | Hosts all three processing tiers, HTTP analyst endpoints, and the dashboard |
| **Azure Blob Storage** | Receives incoming check images; blob upload fires the pipeline trigger |
| **Azure Service Bus** | Decoupled message queue between Tier-1 → Tier-2 → Tier-3 |
| **Azure Cosmos DB** | NoSQL document store for checks, customers, audit log, and fraud cases |
| **Azure Document Intelligence** | OCR and structured field extraction from check images (prebuilt-check model) |
| **Azure OpenAI (GPT-4o)** | Large language model powering the Tier-2 AI investigation agent |
| **Azure Application Insights** | Telemetry, structured logging, and performance monitoring |

---

## AI / Agent Frameworks

Two interchangeable Tier-2 engine implementations, switchable via a single config value (`TIER2_ENGINE`):

| Framework | Engine | Role |
|-----------|--------|------|
| **OpenAI SDK** `openai==2.36.0` | `native` | Hand-rolled ReAct (Reasoning + Acting) loop directly against the Azure OpenAI API |
| **Semantic Kernel** `>=1.17.0` | `sk` | Microsoft's enterprise agent SDK — `ChatCompletionAgent` with a typed `FraudInvestigationPlugin` and a structured 3-phase investigation process |

### Native Engine — ReAct Pattern
The native engine implements the ReAct pattern from scratch:
- A `TOOLS` list with manually crafted JSON schemas
- A `while` loop that drives think → act → observe iterations
- Direct OpenAI API calls with `tool_choice="auto"`
- Manual tool dispatch via `_exec_tool()`

### Semantic Kernel Engine — 3-Phase Structured Process
The SK engine structures the investigation into three explicit phases:

| Phase | Type | What it does |
|-------|------|--------------|
| **ContextStep** | Deterministic | Customer lookup + 24h velocity pre-check before AI starts |
| **InvestigationStep** | Agentic (AI) | `ChatCompletionAgent` with `FraudInvestigationPlugin` — agent autonomously selects tools |
| **VerdictStep** | Deterministic | Structured extraction and validation of the agent's JSON decision |

SK auto-generates tool schemas from Python type annotations on `@kernel_function` decorated methods — no manual JSON schema authoring required.

### Switching Engines
No redeployment needed. Change one setting:

```
# Local development (local.settings.json)
"TIER2_ENGINE": "native"   # or "sk"

# Azure (Function App Settings)
TIER2_ENGINE = sk
```

---

## Azure SDK Libraries

| Library | Version | Purpose |
|---------|---------|---------|
| `azure-functions` | 1.21.3 | Function triggers and HTTP bindings |
| `azure-functions-durable` | 1.5.0 | Blueprint registration pattern |
| `azure-servicebus` | 7.12.3 | Enqueue / dequeue messages across tiers |
| `azure-cosmos` | 4.7.0 | Cosmos DB reads, writes, and parameterized queries |
| `azure-storage-blob` | 12.23.1 | Blob Storage access |
| `azure-ai-formrecognizer` | 3.3.3 | Document Intelligence client |
| `azure-identity` | 1.19.0 | Managed identity and OIDC authentication |
| `azure-keyvault-secrets` | 4.9.0 | Secret retrieval from Azure Key Vault |
| `pydantic` | 2.9.2 | Data validation |

---

## CI/CD & Infrastructure

| Component | Technology | Detail |
|-----------|-----------|--------|
| Source control | GitHub | `main` branch triggers deployment |
| CI/CD | GitHub Actions | Test → Deploy pipeline |
| Azure authentication | OIDC federated credential | No client secrets stored in GitHub |
| Deployment method | `az functionapp deployment source config-zip` | ZIP deploy to Flex Consumption |
| Resource group | `manman-rg` | All resources co-located |
| Region | East US 2 | `eastus2-01` |

### GitHub Actions Pipeline
```
push to main
    │
    ├── test job       → pip install + pytest tests/
    │
    └── deploy job     → az login (OIDC) → zip → az functionapp deploy
```

---

## Architecture Patterns

| Pattern | How it is implemented |
|---------|-----------------------|
| **Event-driven pipeline** | Blob upload → Service Bus queues → HTTP endpoints — no polling |
| **3-tier AI triage** | Rule engine (Tier-1) → AI agent (Tier-2) → Human analyst (Tier-3) |
| **ReAct agent loop** | Native engine: iterative tool-calling against GPT-4o |
| **Structured agentic process** | SK engine: deterministic phases wrap the AI agent loop |
| **Config-driven engine swap** | `TIER2_ENGINE` env var — zero code change, zero redeployment |
| **Human-in-the-loop** | Tier-3 analyst queue with approve/reject via dashboard |
| **Full audit trail** | Every tier decision written to Cosmos `audit_log` container |
| **Parameterized queries** | All Cosmos DB queries use named parameters — no string interpolation |

---

## Cosmos DB Collections

| Container | Contents |
|-----------|----------|
| `checks` | All check records — full lifecycle from pending to analyst decision |
| `customers` | Account profiles, KYC status, synthetic identity flags |
| `audit_log` | Immutable decision log entry per tier per check |
| `fraud_cases` | Known fraud patterns used by the agent's `fraud_pattern_search` tool |

---

## Live Endpoints

| Endpoint | Description |
|----------|-------------|
| `GET /api/dashboard` | Analyst dashboard (HTML) |
| `GET /api/checks/queue` | Checks awaiting analyst review |
| `GET /api/checks/{id}/status` | Status of a specific check |
| `POST /api/checks/{id}/decision` | Submit analyst approve / reject decision |

**Base URL:** `https://checkfraudagent.azurewebsites.net`
