# Check Fraud Detection Agent

AI-powered check fraud detection using Azure Functions, GPT-4o, and a 3-tier architecture.

## Architecture

```
Blob Storage (check image)
    → Blob Trigger Function       # Doc Intelligence extraction
    → Service Bus (tier1-queue)
    → Tier 1 Function             # Rules engine — plain Azure Function
    → Service Bus (tier2-queue)   # Only if escalated
    → Tier 2 Agent Function       # GPT-4o ReAct loop — plain Azure Function
    → Service Bus (tier3-queue)   # Only if escalated
    → Tier 3 Durable Function     # Waits for human analyst decision
```

## Fraud patterns covered

| Pattern | Detection tier | Key signals |
|---|---|---|
| Altered check | Tier 1 | MICR mismatch, amount words vs numeric |
| Structuring | Tier 2 | Multiple checks near $10k threshold |
| Synthetic identity | Tier 2 → Tier 3 | Linked accounts, shared payees, bust-out |

---

## Getting started in GitHub Codespaces

1. Fork this repo on GitHub
2. Click **Code → Codespaces → New codespace**
3. Wait ~2 minutes for the container to build
4. Copy `local.settings.json` and fill in your Azure credentials
5. Run locally: `func start` from the `function_app/` directory

---

## Azure services needed

Provision these manually in the Azure Portal before deploying:

| Service | Purpose |
|---|---|
| Azure Function App (Python 3.11) | Hosts all functions |
| Azure Storage Account | Blob storage for check images |
| Azure Service Bus (Standard) | 3 queues: tier1, tier2, tier3 |
| Azure Cosmos DB (NoSQL) | checks, customers, audit_log containers |
| Azure Document Intelligence | Check field extraction |
| Azure OpenAI | GPT-4o for Tier 2 agent |
| Azure Application Insights | Observability |
| Azure Key Vault | Secrets management |

---

## Cosmos DB setup

Create database `CheckFraudDB` with these containers:

| Container | Partition key |
|---|---|
| checks | /id |
| customers | /account_number |
| audit_log | /check_id |
| fraud_cases | /id |

Seed with synthetic data:
```bash
# From Codespaces terminal
python scripts/seed_cosmos.py
```

---

## GitHub Actions deployment

1. In Azure Portal → Function App → Get publish profile → download
2. In GitHub → Settings → Secrets → add:
   - `AZURE_FUNCTIONAPP_PUBLISH_PROFILE` — paste publish profile XML
   - `AZURE_CREDENTIALS` — Azure service principal JSON
3. Push to `main` → GitHub Actions deploys automatically

---

## API endpoints

| Method | Route | Description |
|---|---|---|
| POST | `/api/checks/{id}/decision` | Analyst submits approve/reject |
| GET | `/api/checks/{id}/status` | Get orchestration status |
| GET | `/api/checks/queue` | Get analyst review queue |

---

## Running tests

```bash
pytest tests/ -v
```

---

## Demo scenarios

| Scenario | Check amount | Expected path |
|---|---|---|
| Clean check | $1,200 | Tier 1 approve in ~200ms |
| Altered check | $14,000 (altered from $2,000) | Tier 1 reject — MICR mismatch |
| Structuring | 15x checks at $8,500–$9,950 | Tier 2 catches pattern, escalates to Tier 3 |
| Synthetic identity | $500 → $15,000 bust-out | Tier 2 connects ring, escalates to Tier 3 |
