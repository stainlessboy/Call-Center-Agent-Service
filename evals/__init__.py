"""Evaluation harness for the LangGraph agent (roadmap П.7б, docs/EVALS.md).

Only the inert pieces are re-exported here — the golden dataset and the pure
evaluators. `evals.runner` and `evals.stubs` are imported explicitly by the
CLI and the tests, because importing them pulls in the whole agent (OpenAI
client, SQLAlchemy models, LangGraph graph) and this package is also imported
by tooling that only wants to read the questions.
"""
from __future__ import annotations

from evals.dataset import (
    ALL_TOOLS,
    GOLDEN_CASES,
    LANGS,
    NO_TOOL,
    accepted_tools,
    coverage_by_language,
    coverage_by_tool,
    iter_cases,
    validate_dataset,
)
from evals.evaluators import (
    category_match,
    default_evaluators,
    evaluate_result,
    language_match,
    latency_under,
    no_error,
    no_pii_leak,
    reached_db,
    tool_match,
)

__all__ = [
    "ALL_TOOLS",
    "GOLDEN_CASES",
    "LANGS",
    "NO_TOOL",
    "accepted_tools",
    "category_match",
    "coverage_by_language",
    "coverage_by_tool",
    "default_evaluators",
    "evaluate_result",
    "iter_cases",
    "language_match",
    "latency_under",
    "no_error",
    "no_pii_leak",
    "reached_db",
    "tool_match",
    "validate_dataset",
]
