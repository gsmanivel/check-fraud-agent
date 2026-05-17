# Check Fraud Agent — Claude Code Instructions

> **Project orientation lives in [README.md](README.md)** — architecture, tier responsibilities, env vars, deployment, scenarios, and project structure. Read it for context.
>
> This file is **rules and conventions for code edits**. Anything below overrides default behavior when modifying this codebase.

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
- `handlers/tier2/native.py` — hand-rolled ReAct loop on `chat.completions.parse()`, returns `{"engine": "native", ...}`
- `handlers/tier2/sk_agent.py` — Semantic Kernel 3-phase process, returns `{"engine": "semantic_kernel", ...}`
- `handlers/tier2/azure_agent.py` — Azure AI Foundry Agent Service (managed runtime), returns `{"engine": "azure_agent", ...}`
- All engines must return the same output contract defined by `FraudDecision` in [handlers/shared/models.py](handlers/shared/models.py): `decision`, `risk_score`, `fraud_pattern`, `fraud_indicators`, `reasoning`, `tool_calls_made`, `iterations`, `engine`. The dispatcher validates the result via `FraudDecision(**raw)` — engines that drift on field names or value ranges will fail there, not silently downstream.
- Switch engine via `TIER2_ENGINE=native`, `sk`, or `azure_agent` — no code changes
- Foundry agent (`azure_agent`) authenticates via `DefaultAzureCredential` only — no API-key fallback. Agent is created lazily on first call and cached by name (`FraudInvestigationAgent`); delete it in the Foundry portal to force recreation after changing `SYSTEM_PROMPT` or tools
- Azure OpenAI API version is read from `AZURE_OPENAI_API_VERSION` (default `2024-10-21`) — never hard-code it in engine files

### Comments
- No comments explaining what code does — well-named identifiers do that
- Only add a comment when the WHY is non-obvious (hidden constraint, workaround, invariant)

## Do Not
- String-interpolate Cosmos queries
- Duplicate velocity logic outside `handlers/shared/velocity.py`
- Commit `local.settings.json`, `.env`, or any secrets
- Add features beyond what the task requires
- Modify `synthetic_data_full.json`
- Skip `write_audit_log()` in any tier decision path
