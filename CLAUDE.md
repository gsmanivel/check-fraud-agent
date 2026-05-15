# Check Fraud Agent — Claude Code Instructions

## Project Overview
Azure Functions v2 Python fraud detection pipeline with 3 processing tiers:
- **Tier-1**: Rule-based scoring engine (MICR, amount, velocity, account checks)
- **Tier-2**: AI investigation agent — dual engine: `native` (ReAct loop) or `sk` (Semantic Kernel)
- **Tier-3**: Human analyst queue with HTTP endpoints and dashboard

Hosted on Azure Flex Consumption Plan. Event-driven via Azure Service Bus queues.

## Project Structure
```
handlers/
  blob_trigger/   ← OCR extraction via Document Intelligence, enqueues to Tier-1
  tier1/          ← Rule engine, scores checks, escalates/approves/rejects
  tier2/          ← AI agent dispatcher (__init__.py), native.py, sk_agent.py
  tier3/          ← Analyst intake, HTTP endpoints, dashboard
  shared/         ← cosmos.py, servicebus.py, velocity.py, utils.py
scripts/          ← Manual dev tools (never deployed)
tests/            ← Automated pytest suite (runs in CI)
docs/             ← Architecture, tech stack, test scenarios, code walkthrough
```

## Code Conventions

### Cosmos DB
- **Always use parameterized queries** — never string interpolation
  ```python
  # Correct
  container.query_items(
      query="SELECT * FROM c WHERE c.id = @id",
      parameters=[{"name": "@id", "value": check_id}],
      enable_cross_partition_query=True
  )
  # Wrong — never do this
  container.query_items(query=f"SELECT * FROM c WHERE c.id = '{check_id}'")
  ```
- Use `get_cosmos_container()` from `handlers/shared/cosmos.py`
- Use `upsert_check()` and `write_audit_log()` for all check updates

### Velocity Checks
- Always use `handlers/shared/velocity.py` — never duplicate velocity logic
- Always pass `exclude_check_id` to avoid self-counting
  ```python
  from handlers.shared.velocity import query_velocity
  data = query_velocity(account_number, days_back=1, exclude_check_id=payload.get("id", ""))
  ```

### Audit Trail
- Every tier decision must call `write_audit_log(check_id, tier, decision, details)`
- This is non-negotiable — audit trail is immutable and required for compliance

### Tier-2 Engines
- `handlers/tier2/__init__.py` — async dispatcher, reads `TIER2_ENGINE` env var
- `handlers/tier2/native.py` — hand-rolled ReAct loop, returns `{"engine": "native", ...}`
- `handlers/tier2/sk_agent.py` — Semantic Kernel 3-phase process, returns `{"engine": "semantic_kernel", ...}`
- Both engines must return the same output contract: `decision`, `fraud_pattern`, `reasoning`, `confidence_score`, `engine`, `tool_calls`, `iterations`
- Switch engine via `TIER2_ENGINE=native` or `TIER2_ENGINE=sk` — no code changes

### Comments
- No comments explaining what code does — well-named identifiers do that
- Only add a comment when the WHY is non-obvious (hidden constraint, workaround, invariant)

## Environment

### Local Development
- Config: `local.settings.json` (never commit — in .gitignore)
- Run locally: `func start`
- Default engine: `TIER2_ENGINE=native`

### Azure Deployment
- Function App: `checkfraudagent-gphqemb0gtfubzgz`
- Resource group: `manman-rg`, Region: `eastus2-01`
- Deploy: `az functionapp deployment source config-zip`
- Base URL: `https://checkfraudagent-gphqemb0gtfubzgz.eastus2-01.azurewebsites.net`

### Key Environment Variables
| Variable | Purpose |
|---|---|
| `TIER2_ENGINE` | `native` or `sk` |
| `AGENT_TIMEOUT_SECONDS` | Max seconds for Tier-2 agent (default 120) |
| `MAX_AGENT_ITERATIONS` | Max ReAct loop iterations (default 10) |
| `COSMOS_CHECKS_CONTAINER` | Cosmos container name for checks |
| `COSMOS_CUSTOMERS_CONTAINER` | Cosmos container name for customers |
| `SERVICE_BUS_CONNECTION_STRING` | Service Bus connection |

## Test Scenarios
Run with: `python scripts/run_scenarios.py --scenario X --engine native/sk --poll`

| Scenario | Account | Expected Path |
|---|---|---|
| A | 4645120421 (Steven Wilson) | Tier-1 APPROVE |
| B | 1478163327 (David Mitchell) | Tier-1 REJECT |
| C | 7857221324 (Ashley Hill) | Tier-1 → Tier-2 (structuring) |
| D | 9190197115 (Patricia Adams) | Tier-1 → Tier-2 (synthetic identity) |
| E | 7803990970 (Linda Allen) | Tier-1 → Tier-2 (new account + large) |
| F | 6237376063 (Anthony Perez) | Tier-1 → Tier-2 → Tier-3 (analyst) |
| G | 1478163327 (David Mitchell) | Engine comparison (run both engines) |
| X1 | 9999999999 | Account not found edge case |

## Do Not
- String-interpolate Cosmos queries
- Duplicate velocity logic outside `handlers/shared/velocity.py`
- Commit `local.settings.json`, `.env`, or any secrets
- Add features beyond what the task requires
- Modify `synthetic_data_full.json`
- Skip `write_audit_log()` in any tier decision path
