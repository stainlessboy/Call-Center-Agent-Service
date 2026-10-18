#!/usr/bin/env python3
"""scripts/eval_agent.py — run the golden dataset and report numbers (roadmap П.7б).

WHY: prompt, model and middleware changes are otherwise judged by eye on a
handful of questions. This runs `evals/dataset.py` through the real agent
graph, scores every turn with `evals/evaluators.py`, and writes both a JSON
report (for the regression gate) and a Markdown summary (for humans).

Usage
-----
    # offline — no keys, no DB, no Weaviate. This is what CI runs on every PR.
    python3 scripts/eval_agent.py
    python3 scripts/eval_agent.py --limit 5 --filter uz

    # live — real OpenAI + real database. The numbers that count.
    python3 scripts/eval_agent.py --live
    python3 scripts/eval_agent.py --live --langsmith
    python3 scripts/eval_agent.py --live --baseline docs/evals/2026-09-04.json

Exit codes: 0 = ok, 1 = regression gate tripped (tool_match fell more than
`--max-drop` percentage points below the baseline), 2 = the run itself broke.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from evals.dataset import GOLDEN_CASES, coverage_by_language, iter_cases, validate_dataset
from evals.evaluators import default_evaluators
from evals.runner import EvalResult, run_dataset

DEFAULT_OUT_DIR = Path(__file__).resolve().parent.parent / "docs" / "evals"
# The roadmap's gate: "доля tool_match не ниже текущего значения минус 2 п.п."
DEFAULT_MAX_DROP_PP = 2.0
GATED_METRIC = "tool_match"
LANGSMITH_DATASET = "asaka-bot-routing"


# ---------------------------------------------------------------------------
# Scoring / aggregation
# ---------------------------------------------------------------------------

def score_results(cases: list[dict], results: list[EvalResult], *,
                  latency_budget_ms: float, product_names: list[str],
                  faq_texts: list[str]) -> tuple[list[dict], dict[str, dict]]:
    """Score every result and aggregate per evaluator.

    Aggregation counts only APPLICABLE verdicts (see evals/evaluators.py):
    a `reached_db` that has nothing to check on a greeting must not count as
    a free pass, or the metric drifts upward as the dataset grows.
    """
    suite = default_evaluators(
        latency_budget_ms=latency_budget_ms,
        product_names=product_names,
        faq_texts=faq_texts,
    )
    scored: list[dict] = []
    summary: dict[str, dict] = {}
    for case, result in zip(cases, results):
        verdicts = [ev(case, result.to_dict()) for ev in suite]
        for verdict in verdicts:
            bucket = summary.setdefault(
                verdict["key"], {"passed": 0, "applicable": 0, "skipped": 0}
            )
            if verdict.get("applicable", True):
                bucket["applicable"] += 1
                if verdict["score"] >= 1.0:
                    bucket["passed"] += 1
            else:
                bucket["skipped"] += 1
        scored.append({"case": case, "result": result.to_dict(), "verdicts": verdicts})

    for bucket in summary.values():
        bucket["rate"] = (
            100.0 * bucket["passed"] / bucket["applicable"] if bucket["applicable"] else 0.0
        )
    return scored, summary


def _totals(scored: list[dict]) -> dict[str, Any]:
    latencies = sorted(row["result"]["latency_ms"] for row in scored) or [0.0]
    return {
        "cases": len(scored),
        "errors": sum(1 for row in scored if row["result"].get("error")),
        "total_cost_usd": round(sum(row["result"].get("cost") or 0.0 for row in scored), 4),
        "total_tokens": sum(
            int((row["result"].get("token_usage") or {}).get("total_tokens") or 0)
            for row in scored
        ),
        "latency_ms_avg": round(sum(latencies) / len(latencies), 1),
        "latency_ms_p95": round(latencies[min(len(latencies) - 1, int(len(latencies) * 0.95))], 1),
    }


# ---------------------------------------------------------------------------
# Console output
# ---------------------------------------------------------------------------

def print_summary(summary: dict[str, dict], totals: dict[str, Any]) -> None:
    print("\n" + "=" * 72)
    print(f"  {'EVALUATOR':<28} {'PASSED':>10} {'RATE':>9} {'N/A':>6}")
    print("=" * 72)
    for key in sorted(summary):
        bucket = summary[key]
        print(f"  {key:<28} {bucket['passed']:>4}/{bucket['applicable']:<5} "
              f"{bucket['rate']:>8.1f}% {bucket['skipped']:>6}")
    print("=" * 72)
    print(f"  cases={totals['cases']}  errors={totals['errors']}  "
          f"avg={totals['latency_ms_avg']:.0f}ms  p95={totals['latency_ms_p95']:.0f}ms  "
          f"tokens={totals['total_tokens']}  cost=${totals['total_cost_usd']:.4f}")


def print_failures(scored: list[dict]) -> None:
    failures = [
        (row, verdict)
        for row in scored
        for verdict in row["verdicts"]
        if verdict.get("applicable", True) and verdict["score"] < 1.0
    ]
    if not failures:
        print("\nNo failures.")
        return
    print(f"\n{'-' * 72}\n  FAILURES ({len(failures)})\n{'-' * 72}")
    for row, verdict in failures:
        case = row["case"]
        print(f"  ✗ [{case['lang']}] {case['id']:<22} {verdict['key']}: {verdict['comment']}")
        print(f"      q: {case['text'][:70]!r}")
        print(f"      a: {row['result']['answer'][:100]!r}")


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------

def build_report(scored: list[dict], summary: dict[str, dict], *, live: bool,
                 latency_budget_ms: float) -> dict[str, Any]:
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": "live" if live else "offline",
        "latency_budget_ms": latency_budget_ms,
        "dataset_size": len(GOLDEN_CASES),
        "cases_run": len(scored),
        "coverage_by_language": coverage_by_language(),
        "summary": summary,
        "totals": _totals(scored),
        "cases": [
            {
                "id": row["case"]["id"],
                "lang": row["case"]["lang"],
                "text": row["case"]["text"],
                "expected_tool": row["case"]["expected_tool"],
                "expected_category": row["case"]["expected_category"],
                "must_reach_db": row["case"]["must_reach_db"],
                "tools": row["result"]["tools"],
                "answer": row["result"]["answer"],
                "latency_ms": round(row["result"]["latency_ms"], 1),
                "steps": row["result"]["steps"],
                "token_usage": row["result"]["token_usage"],
                "cost": row["result"]["cost"],
                "error": row["result"]["error"],
                "verdicts": row["verdicts"],
            }
            for row in scored
        ],
    }


def render_markdown(report: dict[str, Any]) -> str:
    totals = report["totals"]
    lines = [
        f"# Отчёт evals — {report['generated_at'][:10]}",
        "",
        f"Режим: **{report['mode']}** · кейсов прогнано: {report['cases_run']} из "
        f"{report['dataset_size']} · ошибок: {totals['errors']}",
        f"Задержка: avg {totals['latency_ms_avg']:.0f} мс, p95 {totals['latency_ms_p95']:.0f} мс "
        f"(бюджет {report['latency_budget_ms']:.0f} мс) · токенов {totals['total_tokens']} · "
        f"стоимость ${totals['total_cost_usd']:.4f}",
        "",
        "## Сводка по эвалуаторам",
        "",
        "| Эвалуатор | Прошло | Доля | Неприменимо |",
        "|---|---:|---:|---:|",
    ]
    for key in sorted(report["summary"]):
        bucket = report["summary"][key]
        lines.append(
            f"| `{key}` | {bucket['passed']}/{bucket['applicable']} | "
            f"{bucket['rate']:.1f}% | {bucket['skipped']} |"
        )

    failures = [
        (case, verdict)
        for case in report["cases"]
        for verdict in case["verdicts"]
        if verdict.get("applicable", True) and verdict["score"] < 1.0
    ]
    lines += ["", f"## Провалы ({len(failures)})", ""]
    if not failures:
        lines.append("Нет.")
    else:
        lines += ["| Кейс | Язык | Эвалуатор | Комментарий | Вопрос |", "|---|---|---|---|---|"]
        for case, verdict in failures:
            comment = str(verdict["comment"]).replace("|", "\\|")[:120]
            text = case["text"].replace("|", "\\|")[:60]
            lines.append(
                f"| `{case['id']}` | {case['lang']} | `{verdict['key']}` | {comment} | {text} |"
            )

    lines += ["", "## Инструменты по кейсам", "",
              "| Кейс | Ожидали | Вызвано | Шагов | мс |", "|---|---|---|---:|---:|"]
    for case in report["cases"]:
        expected = case["expected_tool"]
        expected_str = "—" if expected is None else (
            expected if isinstance(expected, str) else ", ".join(expected)
        )
        called = ", ".join(case["tools"]) or "—"
        lines.append(
            f"| `{case['id']}` | {expected_str} | {called} | {case['steps']} | "
            f"{case['latency_ms']:.0f} |"
        )
    lines.append("")
    return "\n".join(lines)


def write_reports(report: dict[str, Any], out_dir: Path, tag: Optional[str]) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = date.today().isoformat() + (f"-{tag}" if tag else "")
    json_path = out_dir / f"{stem}.json"
    md_path = out_dir / f"{stem}.md"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(render_markdown(report), encoding="utf-8")
    return json_path, md_path


# ---------------------------------------------------------------------------
# Regression gate
# ---------------------------------------------------------------------------

def check_baseline(report: dict[str, Any], baseline_path: Path,
                   max_drop_pp: float) -> tuple[bool, str]:
    """Compare `tool_match` against a previous report.

    Percentage POINTS, not percent: a drop from 90% to 88% is 2 p.p. and
    passes, 90% → 87% is 3 p.p. and fails. Comparing relative percentages
    would make the gate stricter at low scores and looser at high ones, which
    is backwards.
    """
    try:
        baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        return False, f"baseline {baseline_path} could not be read: {exc}"

    old = (baseline.get("summary") or {}).get(GATED_METRIC) or {}
    new = (report.get("summary") or {}).get(GATED_METRIC) or {}
    if not old or not new:
        return False, f"baseline or current report has no {GATED_METRIC} metric"

    old_rate, new_rate = float(old.get("rate", 0.0)), float(new.get("rate", 0.0))
    drop = old_rate - new_rate
    message = (
        f"{GATED_METRIC}: baseline {old_rate:.1f}% → current {new_rate:.1f}% "
        f"({-drop:+.1f} p.p., allowed drop {max_drop_pp:.1f} p.p.)"
    )
    if baseline.get("mode") != report.get("mode"):
        message += (
            f"  [warning: baseline mode={baseline.get('mode')!r} differs from "
            f"current mode={report.get('mode')!r} — the comparison is not apples to apples]"
        )
    return drop <= max_drop_pp, message


# ---------------------------------------------------------------------------
# LangSmith upload
# ---------------------------------------------------------------------------

def upload_to_langsmith(scored: list[dict], *, dataset_name: str,
                        experiment_prefix: str) -> Optional[str]:
    """Push this run to LangSmith as an experiment (roadmap П.7б, step 2).

    Imported lazily and guarded on `LANGSMITH_API_KEY` so the script is fully
    usable without the SDK or an account. The target function replays the
    results we ALREADY computed instead of running the agent a second time —
    the numbers in the LangSmith experiment are then exactly the numbers in
    the local report, and a live run is paid for once.
    """
    if not os.getenv("LANGSMITH_API_KEY"):
        print("--langsmith ignored: LANGSMITH_API_KEY is not set")
        return None
    try:
        from langsmith import Client, evaluate
    except ImportError:
        print("--langsmith ignored: the `langsmith` package is not installed")
        return None

    try:
        client = Client()
        by_id = {row["case"]["id"]: row for row in scored}
        if not client.has_dataset(dataset_name=dataset_name):
            dataset = client.create_dataset(
                dataset_name=dataset_name,
                description="Golden routing/quality cases for the Asaka banking agent "
                            "(source of truth: evals/dataset.py).",
            )
            client.create_examples(
                dataset_id=dataset.id,
                inputs=[
                    {"id": row["case"]["id"], "text": row["case"]["text"],
                     "lang": row["case"]["lang"], "dialog": row["case"]["dialog"]}
                    for row in scored
                ],
                outputs=[
                    {"expected_tool": row["case"]["expected_tool"],
                     "expected_category": row["case"]["expected_category"],
                     "must_reach_db": row["case"]["must_reach_db"]}
                    for row in scored
                ],
            )

        def target(inputs: dict) -> dict:
            row = by_id.get(inputs.get("id"))
            return row["result"] if row else {"answer": "", "tools": []}

        def _make_evaluator(key: str):
            def _evaluator(inputs: dict, outputs: dict) -> dict:  # noqa: ARG001
                row = by_id.get(inputs.get("id"))
                if not row:
                    return {"key": key, "score": 0.0, "comment": "case not found"}
                for verdict in row["verdicts"]:
                    if verdict["key"] == key:
                        return {"key": key, "score": verdict["score"],
                                "comment": verdict["comment"]}
                return {"key": key, "score": 0.0, "comment": "no verdict"}

            _evaluator.__name__ = key
            return _evaluator

        keys = sorted({v["key"] for row in scored for v in row["verdicts"]})
        results = evaluate(
            target,
            data=dataset_name,
            evaluators=[_make_evaluator(key) for key in keys],
            experiment_prefix=experiment_prefix,
        )
        name = getattr(results, "experiment_name", None) or experiment_prefix
        print(f"LangSmith experiment: {name} (dataset {dataset_name})")
        return str(name)
    except Exception as exc:  # noqa: BLE001 — reporting must never fail the run
        print(f"--langsmith upload failed: {type(exc).__name__}: {exc}")
        return None


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _progress(index: int, total: int, case: dict, result: EvalResult) -> None:
    mark = "!" if result.error else "·"
    tools = ",".join(result.tools) or "—"
    print(f"  {mark} [{index:>3}/{total}] {case['lang']} {case['id']:<22} "
          f"{tools:<30} {result.latency_ms:>7.0f}ms")


async def _amain(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Run the agent evaluation harness")
    parser.add_argument("--live", action="store_true",
                        help="use the real OpenAI API and database (default: offline stubs)")
    parser.add_argument("--limit", type=int, default=None, help="run only the first N cases")
    parser.add_argument("--filter", dest="name_filter", default=None,
                        help="substring of the case id / text, a language, or a tool name")
    parser.add_argument("--langsmith", action="store_true",
                        help="upload the run as a LangSmith experiment (needs LANGSMITH_API_KEY)")
    parser.add_argument("--baseline", type=Path, default=None,
                        help="previous JSON report to gate against")
    parser.add_argument("--max-drop", type=float, default=DEFAULT_MAX_DROP_PP,
                        help=f"allowed {GATED_METRIC} drop in percentage points "
                             f"(default {DEFAULT_MAX_DROP_PP})")
    parser.add_argument("--latency-ms", type=float, default=8000.0,
                        help="latency budget per case in ms (default 8000, per the roadmap)")
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument("--tag", default=None,
                        help="suffix for the report file names, e.g. --tag ci-offline")
    parser.add_argument("--no-report", action="store_true", help="skip writing report files")
    args = parser.parse_args(argv)

    problems = validate_dataset()
    if problems:
        print("Dataset is invalid:")
        for problem in problems:
            print(f"  - {problem}")
        return 2

    cases = list(iter_cases(limit=args.limit, name_filter=args.name_filter))
    if not cases:
        print("No cases matched the filter.")
        return 2

    mode = "LIVE (real OpenAI + DB)" if args.live else "OFFLINE (stubbed LLM + DB)"
    print(f"Mode: {mode}\nCases: {len(cases)} of {len(GOLDEN_CASES)}\n")

    results, harness_ctx = await run_dataset(cases, live=args.live, progress=_progress)

    scored, summary = score_results(
        cases, results,
        latency_budget_ms=args.latency_ms,
        product_names=harness_ctx.product_names,
        faq_texts=harness_ctx.faq_texts,
    )
    totals = _totals(scored)
    print_summary(summary, totals)
    print_failures(scored)

    report = build_report(scored, summary, live=args.live, latency_budget_ms=args.latency_ms)
    if args.langsmith:
        report["langsmith_experiment"] = upload_to_langsmith(
            scored,
            dataset_name=os.getenv("LANGSMITH_EVAL_DATASET") or LANGSMITH_DATASET,
            experiment_prefix=f"asaka-agent-{'live' if args.live else 'offline'}",
        )

    if not args.no_report:
        json_path, md_path = write_reports(report, args.out_dir, args.tag)
        print(f"\nReports: {json_path}\n         {md_path}")

    if args.baseline:
        ok, message = check_baseline(report, args.baseline, args.max_drop)
        print(f"\nRegression gate: {message}")
        if not ok:
            print("FAILED: tool_match regressed beyond the allowed drop.")
            return 1
        print("PASSED")
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    return asyncio.run(_amain(argv))


if __name__ == "__main__":
    raise SystemExit(main())
