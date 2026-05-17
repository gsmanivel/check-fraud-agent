# Tier-2 Agent Evaluation Harness

Measures whether the Tier-2 fraud-detection agent makes the right decisions on a labeled set of checks. Runs the real agent (real LLM calls to Azure OpenAI) against a golden set with **canned tool outputs**, so results are reproducible and not coupled to whatever's in Cosmos DB today.

## Why this exists

Before this harness, every change to the agent — prompt edits, new tools, engine swaps, model upgrades — was unmeasurable. With it, every change is scored against the same set of expectations and the deltas are visible.

## Files

| File | Purpose |
|---|---|
| [golden_set.json](golden_set.json) | Labeled cases with `payload` + `expected` (decision, fraud_pattern, must-call tools, indicator keywords) |
| [fixtures.py](fixtures.py) | Canned customers / velocity / fraud_cases; `install()` monkeypatches the cosmos+velocity functions in both engine modules |
| [run_eval.py](run_eval.py) | CLI runner — invokes the agent for each case, scores, emits a JSON report |
| [test_eval_smoke.py](test_eval_smoke.py) | Framework tests (no LLM) — gold-set schema, fixture wiring, scoring logic |
| [../../.github/workflows/eval.yml](../../.github/workflows/eval.yml) | Manual `workflow_dispatch` workflow with real OpenAI secrets |

## Run locally

Requires `local.settings.json` with `AZURE_OPENAI_*` values (the runner auto-loads it).

```bash
# Native engine, full golden set
python tests/eval/run_eval.py --engine native

# SK engine, specific cases
python tests/eval/run_eval.py --engine sk --cases structuring_classic synthetic_identity_with_kyc_gap

# Side-by-side: both engines, same golden set
python tests/eval/run_eval.py --engine both
```

Reports land in `tests/eval/reports/eval_<engine>_<timestamp>.json`.

## Run in CI

GitHub Actions → "Agent Eval" → Run workflow. Pick engine + min accuracy. The job uploads the JSON report as an artifact (retained 30 days).

Required GitHub secrets:
- `AZURE_OPENAI_ENDPOINT`
- `AZURE_OPENAI_KEY`
- `AZURE_OPENAI_DEPLOYMENT`

## What's measured

The runner emits per-engine metrics:

| Metric | Meaning |
|---|---|
| `overall_accuracy` | Fraction of cases passing **all** scoring criteria (decision + pattern + tools) |
| `decision_accuracy` | Fraction with `decision ∈ expected.decision_in` |
| `pattern_accuracy` | Fraction with `fraud_pattern ∈ expected.fraud_pattern_in` |
| `tools_compliance` | Fraction where every `must_call_tools` entry was actually called |
| `keyword_recall` | Fraction whose indicators+reasoning contain at least one `indicator_keyword` |
| `avg_iterations` | Mean tool-call iterations per case |
| `p50_latency_ms` / `max_latency_ms` | Per-case end-to-end timing |
| `n_errors` | Cases where the agent raised an exception |

## Adding a new case

1. Identify the fraud scenario, gather a representative payload
2. If the case uses a new account, add it to `CUSTOMERS` in [fixtures.py](fixtures.py)
3. If the case needs specific velocity behavior, add to `VELOCITY_DATA`
4. Append an entry to `cases` in [golden_set.json](golden_set.json) with:
   - **`payload`** — shape matches what arrives at Tier-2 after Tier-1
   - **`expected.decision_in`** — usually `["escalate"]` or `["escalate", "reject"]`; use `["approve"]` only for clearly clean cases
   - **`expected.fraud_pattern_in`** — include `null` if pattern is acceptably absent
   - **`expected.must_call_tools`** — tools the agent *must* call; keep small (e.g., always `customer_lookup`)
   - **`expected.indicator_keywords`** — lowercase substrings expected somewhere in indicators or reasoning

5. Run locally to verify the case is achievable (and tune expectations if the agent does the right thing but in a slightly different way)

## What this *doesn't* cover yet

- **Tier-1 rules** — unit tests in `tests/test_tier1.py` already cover those
- **End-to-end via Service Bus** — `scripts/run_scenarios.py` does that manually
- **Cost tracking** — token usage isn't aggregated yet (TODO)
- **Per-case statistical confidence** — sample size is small; treat as smoke + regression, not strict performance benchmark
- **Adversarial / prompt-injection cases** — covered separately by Content Safety Prompt Shields (N4)

## Migration to azure-ai-evaluation

Eventually swap the hand-rolled scoring in `_score_case` for the `azure-ai-evaluation` SDK's evaluators (decision accuracy, retrieval scores, custom evaluators). For now the simpler approach lets us own the criteria.
