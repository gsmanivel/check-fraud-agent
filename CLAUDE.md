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
- `handlers/tier2/native.py` — hand-rolled ReAct loop, returns `{"engine": "native", ...}`
- `handlers/tier2/sk_agent.py` — Semantic Kernel 3-phase process, returns `{"engine": "semantic_kernel", ...}`
- Both engines must return the same output contract: `decision`, `fraud_pattern`, `reasoning`, `confidence_score`, `engine`, `tool_calls`, `iterations`
- Switch engine via `TIER2_ENGINE=native` or `TIER2_ENGINE=sk` — no code changes

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
