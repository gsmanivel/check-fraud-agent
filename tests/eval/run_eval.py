"""
Tier-2 agent evaluation runner.

Loads tests/eval/golden_set.json, invokes the chosen engine against each case,
scores the result against per-case expectations, and emits a JSON report
(stdout + tests/eval/reports/<timestamp>.json).

Reproducibility: tool implementations (customer_lookup, velocity_check,
fraud_pattern_search) are monkeypatched via tests/eval/fixtures.py so they
return canned data instead of hitting live Cosmos. The LLM still calls real
Azure OpenAI for genuine agent-behavior measurement.

Usage:
  python tests/eval/run_eval.py --engine native
  python tests/eval/run_eval.py --engine azure_agent
  python tests/eval/run_eval.py --report-dir tests/eval/reports

Exit codes:
  0  decision_accuracy >= --min-accuracy (default 0.6)
  1  below threshold (CI gate)
  2  config / IO error
"""
import argparse
import asyncio
import json
import os
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

# Load env vars from local.settings.json if present (parity with run_scenarios.py).
_local_settings = ROOT / "local.settings.json"
if _local_settings.exists():
    settings = json.loads(_local_settings.read_text())["Values"]
    for k, v in settings.items():
        os.environ.setdefault(k, str(v))

from tests.eval import fixtures  # noqa: E402


def _score_case(result: dict, expected: dict) -> dict:
    """Compare agent result against expectations. Returns per-criterion bools."""
    decision       = result.get("decision")
    fraud_pattern  = result.get("fraud_pattern")
    tools_called   = {t["tool"] for t in result.get("tool_calls_made", [])}
    indicators     = " ".join(result.get("fraud_indicators", [])).lower()
    reasoning      = (result.get("reasoning") or "").lower()
    blob           = indicators + " " + reasoning

    decision_match = decision in expected.get("decision_in", [])

    expected_patterns = expected.get("fraud_pattern_in", [])
    pattern_match     = fraud_pattern in expected_patterns if expected_patterns else True

    must_tools    = set(expected.get("must_call_tools", []))
    tools_match   = must_tools.issubset(tools_called)
    missing_tools = sorted(must_tools - tools_called)

    keywords       = expected.get("indicator_keywords", [])
    keyword_matches = [kw for kw in keywords if kw.lower() in blob]
    keyword_match  = (not keywords) or len(keyword_matches) > 0

    return {
        "decision_match":  decision_match,
        "pattern_match":   pattern_match,
        "tools_match":     tools_match,
        "missing_tools":   missing_tools,
        "keyword_match":   keyword_match,
        "keyword_matches": keyword_matches,
        "overall_pass":    decision_match and pattern_match and tools_match,
    }


async def _run_one(engine: str, case: dict) -> dict:
    """Run a single case against the chosen engine and score the result."""
    payload    = dict(case["payload"])
    expected   = case["expected"]
    case_start = time.time()

    try:
        if engine == "azure_agent":
            from handlers.tier2.azure_agent import run_agent_foundry
            result = await run_agent_foundry(payload, case_start)
        else:
            from handlers.tier2.native import run_agent_native
            result = run_agent_native(payload, case_start)
        error = None
    except Exception as exc:
        result = {
            "decision":        "error",
            "risk_score":      0,
            "fraud_pattern":   None,
            "fraud_indicators": [],
            "reasoning":       str(exc),
            "tool_calls_made": [],
            "iterations":      0,
            "engine":          engine,
        }
        error = str(exc)

    elapsed_ms = int((time.time() - case_start) * 1000)
    score      = _score_case(result, expected)

    return {
        "case_id":           case["id"],
        "engine":            engine,
        "elapsed_ms":        elapsed_ms,
        "decision":          result.get("decision"),
        "fraud_pattern":     result.get("fraud_pattern"),
        "iterations":        result.get("iterations", 0),
        "tools_called":      [t["tool"] for t in result.get("tool_calls_made", [])],
        "fraud_indicators":  result.get("fraud_indicators", []),
        "reasoning_excerpt": (result.get("reasoning") or "")[:300],
        "expected":          expected,
        "score":             score,
        "error":             error,
    }


def _aggregate(case_results: list[dict]) -> dict:
    if not case_results:
        return {}

    total           = len(case_results)
    n_pass          = sum(1 for r in case_results if r["score"]["overall_pass"])
    n_decision_pass = sum(1 for r in case_results if r["score"]["decision_match"])
    n_pattern_pass  = sum(1 for r in case_results if r["score"]["pattern_match"])
    n_tools_pass    = sum(1 for r in case_results if r["score"]["tools_match"])
    n_keyword_pass  = sum(1 for r in case_results if r["score"]["keyword_match"])
    n_errors        = sum(1 for r in case_results if r.get("error"))

    latencies  = [r["elapsed_ms"] for r in case_results]
    iterations = [r["iterations"] for r in case_results if r["iterations"]]

    return {
        "n_cases":           total,
        "n_pass":            n_pass,
        "n_errors":          n_errors,
        "overall_accuracy":  round(n_pass / total, 3),
        "decision_accuracy": round(n_decision_pass / total, 3),
        "pattern_accuracy":  round(n_pattern_pass / total, 3),
        "tools_compliance":  round(n_tools_pass / total, 3),
        "keyword_recall":    round(n_keyword_pass / total, 3),
        "avg_iterations":    round(statistics.mean(iterations), 2) if iterations else 0,
        "p50_latency_ms":    int(statistics.median(latencies)) if latencies else 0,
        "p95_latency_ms":    int(statistics.quantiles(latencies, n=20)[18]) if len(latencies) >= 20 else max(latencies, default=0),
        "max_latency_ms":    max(latencies, default=0),
    }


async def run_eval(engine: str, case_ids: list[str] | None, golden_path: Path) -> dict:
    fixtures.install()  # monkeypatch cosmos/velocity globally for this process

    golden = json.loads(golden_path.read_text())
    cases  = golden["cases"]

    if case_ids:
        wanted = set(case_ids)
        cases  = [c for c in cases if c["id"] in wanted]
        if not cases:
            raise SystemExit(f"No cases matched: {case_ids}")

    print(f"Running {len(cases)} case(s) against engine='{engine}'...", file=sys.stderr)

    case_results = []
    for i, case in enumerate(cases, 1):
        print(f"  [{i}/{len(cases)}] {case['id']}...", end=" ", file=sys.stderr, flush=True)
        r = await _run_one(engine, case)
        case_results.append(r)
        status = "✓" if r["score"]["overall_pass"] else ("✗" if not r.get("error") else "ERR")
        print(f"{status} ({r['elapsed_ms']}ms, decision={r['decision']})", file=sys.stderr)

    return {
        "run_id":         datetime.now(timezone.utc).isoformat(),
        "engine":         engine,
        "golden_version": golden.get("version"),
        "metrics":        _aggregate(case_results),
        "case_results":   case_results,
    }


def main():
    parser = argparse.ArgumentParser(description="Tier-2 agent evaluation runner")
    parser.add_argument(
        "--engine",
        choices=["native", "azure_agent"],
        default="native",
        help="Which Tier-2 engine to evaluate",
    )
    parser.add_argument("--cases",       nargs="+", help="Run only these case IDs")
    parser.add_argument("--golden-set",  default="tests/eval/golden_set.json",
                        help="Path to golden_set.json")
    parser.add_argument("--report-dir",  default="tests/eval/reports",
                        help="Directory to write timestamped JSON reports")
    parser.add_argument("--min-accuracy", type=float, default=0.6,
                        help="Minimum decision_accuracy required (exit 1 if below)")
    parser.add_argument("--no-report-file", action="store_true",
                        help="Print to stdout only, don't write a report file")
    args = parser.parse_args()

    golden_path = Path(args.golden_set)
    if not golden_path.exists():
        print(f"Golden set not found: {golden_path}", file=sys.stderr)
        sys.exit(2)

    report     = asyncio.run(run_eval(args.engine, args.cases, golden_path))
    all_reports = [report]
    overall_min = report["metrics"]["decision_accuracy"]

    m = report["metrics"]
    print(file=sys.stderr)
    print(f"=== {args.engine} ===", file=sys.stderr)
    print(f"  overall_accuracy:  {m['overall_accuracy']}",  file=sys.stderr)
    print(f"  decision_accuracy: {m['decision_accuracy']}", file=sys.stderr)
    print(f"  pattern_accuracy:  {m['pattern_accuracy']}",  file=sys.stderr)
    print(f"  tools_compliance:  {m['tools_compliance']}",  file=sys.stderr)
    print(f"  avg_iterations:    {m['avg_iterations']}",    file=sys.stderr)
    print(f"  p50_latency_ms:    {m['p50_latency_ms']}",    file=sys.stderr)
    print(f"  max_latency_ms:    {m['max_latency_ms']}",    file=sys.stderr)
    print(f"  errors:            {m['n_errors']}",          file=sys.stderr)

    print(json.dumps(report, indent=2, default=str))

    if not args.no_report_file:
        out_dir  = Path(args.report_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        stamp    = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        out_path = out_dir / f"eval_{args.engine}_{stamp}.json"
        out_path.write_text(json.dumps(report, indent=2, default=str))
        print(f"\nReport written: {out_path}", file=sys.stderr)

    if overall_min < args.min_accuracy:
        print(f"\nFAIL: decision_accuracy {overall_min} < threshold {args.min_accuracy}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()