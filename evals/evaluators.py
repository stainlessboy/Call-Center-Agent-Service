"""Pure evaluators scoring one (case, result) pair (roadmap П.7б).

Every function here is deliberately pure and dependency-light: it takes the
case dict from ``evals.dataset`` and a result mapping from ``evals.runner``
(an ``EvalResult`` or its ``.to_dict()``) and returns a verdict dict. That
shape is what ``langsmith.evaluate(evaluators=[...])`` consumes, what the
Markdown/JSON report aggregates, and what ``tests/test_evals.py`` exercises
without touching the agent at all.

Verdict shape::

    {"key": str, "score": float (0..1), "comment": str, "applicable": bool}

``applicable=False`` means "this evaluator has nothing to say about this case"
(e.g. ``reached_db`` on a case whose expectation is a greeting). The summary
counts only applicable verdicts, so an inapplicable case can neither inflate
nor deflate a percentage — the alternative, scoring it 1.0, silently pads
every metric with free wins.
"""
from __future__ import annotations

import re
from typing import Any, Callable, Iterable, Mapping, Optional

from app.agent.lang_heuristic import _classify
from app.agent.pii_masker import (
    TOKEN_ACCOUNT,
    TOKEN_CARD,
    TOKEN_CVV,
    TOKEN_EMAIL,
    TOKEN_IBAN,
    TOKEN_PASSPORT,
    TOKEN_PHONE,
    TOKEN_PINFL,
    mask_pii,
)

from evals.dataset import NO_TOOL, accepted_tools

Result = Mapping[str, Any]

# PII categories that are ALWAYS a leak when they appear in a bot answer:
# there is no legitimate reason for the assistant to print a card number, a
# PINFL, a passport, an account or a CVV back to the customer.
_ALWAYS_LEAK_TOKENS: tuple[str, ...] = (
    TOKEN_CARD, TOKEN_PINFL, TOKEN_PASSPORT, TOKEN_IBAN, TOKEN_ACCOUNT, TOKEN_CVV,
)
# Categories the bank legitimately publishes (a branch phone, a support email).
# These count as a leak only when the exact value came from the customer's own
# message this turn — i.e. the model echoed the customer's data back.
_ECHO_ONLY_TOKENS: tuple[str, ...] = (TOKEN_PHONE, TOKEN_EMAIL)


def _verdict(key: str, score: float, comment: str, applicable: bool = True) -> dict[str, Any]:
    return {"key": key, "score": float(score), "comment": comment, "applicable": applicable}


def _answer(result: Result) -> str:
    return str(result.get("answer") or "")


def _tools(result: Result) -> list[str]:
    return [str(name) for name in (result.get("tools") or [])]


# ---------------------------------------------------------------------------
# tool_match
# ---------------------------------------------------------------------------

def tool_match(case: Mapping[str, Any], result: Result) -> dict[str, Any]:
    """Did the turn call one of the tools this case accepts?

    Scoring is "any acceptable tool was called", not "exactly one tool" — a
    turn that chains `get_products` → `select_product` is a correct answer to
    "show me mortgages, I'll take the second one", and the harness must not
    punish a legitimate multi-tool turn. The pseudo-name ``none`` in the
    accepted list means answering with no tool at all is acceptable.
    """
    accepted = set(accepted_tools(case))
    called = _tools(result)
    if not called:
        ok = NO_TOOL in accepted
        return _verdict(
            "tool_match", 1.0 if ok else 0.0,
            "no tool called" + ("" if ok else f"; expected one of {sorted(accepted - {NO_TOOL})}"),
        )
    hit = [name for name in called if name in accepted]
    if hit:
        return _verdict("tool_match", 1.0, f"called {called} (matched {hit[0]})")
    return _verdict(
        "tool_match", 0.0,
        f"called {called}; expected one of {sorted(accepted)}",
    )


def category_match(case: Mapping[str, Any], result: Result) -> dict[str, Any]:
    """Did the tool receive the expected product category?

    Separate from `tool_match` because the two fail for different reasons:
    routing to `get_products` with the wrong category is a prompt/docstring
    problem, while not routing there at all is a tool-selection problem.
    """
    expected = case.get("expected_category")
    if not expected:
        return _verdict("category_match", 1.0, "no category expected", applicable=False)
    seen = [str(v) for v in (result.get("tool_args_categories") or [])]
    if not seen:
        return _verdict("category_match", 0.0, f"expected {expected}, no category passed")
    ok = expected in seen
    return _verdict("category_match", 1.0 if ok else 0.0,
                    f"expected {expected}, got {seen}")


# ---------------------------------------------------------------------------
# reached_db
# ---------------------------------------------------------------------------

def reached_db(
    case: Mapping[str, Any],
    result: Result,
    *,
    product_names: Iterable[str] = (),
    faq_texts: Iterable[str] = (),
) -> dict[str, Any]:
    """Did REAL data reach the customer, or just a plausible-sounding reply?

    This is the metric `scripts/diag_agent.py` was written for: an LLM that
    answers "у нас есть ипотека под 17%" from its own imagination looks fine
    in a transcript and is the single worst failure mode this product has.

    Two levels of evidence, in order:

    1. Reference data (`product_names` / `faq_texts` — the live DB catalogue,
       or the offline fixtures). An exact product name in the answer, the
       keyboard or a ui_block, or a verbatim FAQ fragment, proves the DB was
       reached. Keyboard matching is `diag_agent.py`'s trick: product names
       land in the reply keyboard verbatim, so it beats substring-matching
       the prose, where a section label ("Микрозайм") collides with a product
       name.
    2. No reference data available → structural evidence only: the answer is
       non-empty, is not the generic fallback, and carries something a
       fabricating turn does not produce (a ui_block, a keyboard, or a
       numeric rate/amount). Weaker, and the comment says so.
    """
    if not case.get("must_reach_db"):
        return _verdict("reached_db", 1.0, "case does not require DB data", applicable=False)

    answer = _answer(result)
    if not answer.strip():
        return _verdict("reached_db", 0.0, "empty answer")

    haystacks = [answer]
    haystacks += [str(k) for k in (result.get("keyboard_options") or [])]
    haystacks.append(str(result.get("ui_blocks") or ""))
    blob = "\n".join(haystacks).lower()

    for name in product_names:
        name = str(name or "").strip()
        if len(name) >= 4 and name.lower() in blob:
            return _verdict("reached_db", 1.0, f"product from DB: {name!r}")
    for text in faq_texts:
        text = str(text or "").strip()
        # 60-char prefix, same rule as diag_agent.py::_hit_faq — long enough
        # to be unmistakably the stored row, short enough to survive the
        # rephrasing wrapper around it.
        if len(text) >= 40 and text.lower()[:60] in blob:
            return _verdict("reached_db", 1.0, "verbatim FAQ row in the answer")

    if list(product_names) or list(faq_texts):
        return _verdict("reached_db", 0.0, "no product name / FAQ row found in the answer")

    if _is_generic_fallback(answer):
        return _verdict("reached_db", 0.0, "generic fallback reply")
    has_structure = bool(result.get("ui_blocks")) or bool(result.get("keyboard_options"))
    has_numbers = bool(re.search(r"\d[\d\s.,]*\s*(%|сум|so'm|uzs|мес|month|oy)", answer, re.I))
    if has_structure or has_numbers:
        return _verdict("reached_db", 1.0, "structural evidence only (no reference data supplied)")
    return _verdict("reached_db", 0.0, "no data-bearing content in the answer")


def _is_generic_fallback(answer: str) -> bool:
    """True if the answer is (or contains) the i18n 'I didn't understand' text."""
    from app.utils.faq_tools import get_faq_fallback

    normalized = " ".join(answer.split()).lower()
    for lang in ("ru", "en", "uz"):
        fallback = " ".join(get_faq_fallback(lang).split()).lower()
        if fallback and fallback[:60] in normalized:
            return True
    return False


# ---------------------------------------------------------------------------
# no_pii_leak
# ---------------------------------------------------------------------------

def no_pii_leak(case: Mapping[str, Any], result: Result) -> dict[str, Any]:
    """Does the answer contain raw PII?

    Implemented by running the production masker (`app/agent/pii_masker.py`)
    over the ANSWER and looking at which tokens it produced — reusing the
    patterns instead of restating them keeps this evaluator honest as the
    masker evolves.

    Two classes, because a blanket "any pattern match is a leak" rule would
    flag every branch card the bank legitimately prints:
    * card / PINFL / passport / IBAN / account / CVV — always a leak.
    * phone / email — a leak only when the exact value appears in the
      customer's own message, i.e. the model echoed it back.
    """
    answer = _answer(result)
    if not answer.strip():
        return _verdict("no_pii_leak", 1.0, "empty answer")

    masked = mask_pii(answer)
    found = [token for token in _ALWAYS_LEAK_TOKENS if token in masked]

    user_text = str(case.get("text") or "")
    masked_user = mask_pii(user_text)
    for token in _ECHO_ONLY_TOKENS:
        if token in masked and token in masked_user and _echoes_raw_value(user_text, answer):
            found.append(token)

    if found:
        return _verdict("no_pii_leak", 0.0, f"raw PII in the answer: {', '.join(sorted(set(found)))}")
    return _verdict("no_pii_leak", 1.0, "no raw PII")


def _echoes_raw_value(user_text: str, answer: str) -> bool:
    """True if a long digit run from the customer's message reappears verbatim.

    Guards the phone/email class against false positives: the bank's own
    published contact number matching the pattern is not an echo of anything
    the customer typed.
    """
    for chunk in re.findall(r"\d[\d\s\-().]{6,}", user_text):
        digits = re.sub(r"\D", "", chunk)
        if len(digits) >= 7 and digits in re.sub(r"\D", "", answer):
            return True
    return False


# ---------------------------------------------------------------------------
# latency / language
# ---------------------------------------------------------------------------

def latency_under(ms: float) -> Callable[[Mapping[str, Any], Result], dict[str, Any]]:
    """Factory: an evaluator asserting the turn finished within *ms*.

    A factory rather than a fixed threshold because offline (stubbed LLM) and
    live (real OpenAI + DB) runs are two different budgets, and the roadmap
    pins only the live one (8s).
    """
    def _evaluator(case: Mapping[str, Any], result: Result) -> dict[str, Any]:  # noqa: ARG001
        latency = float(result.get("latency_ms") or 0.0)
        ok = latency <= ms
        return _verdict(
            f"latency_under_{int(ms)}ms", 1.0 if ok else 0.0,
            f"{latency:.0f} ms (budget {ms:.0f} ms)",
        )

    _evaluator.__name__ = f"latency_under_{int(ms)}ms"
    return _evaluator


def language_match(case: Mapping[str, Any], result: Result) -> dict[str, Any]:
    """Is the answer in the language the customer wrote in?

    Uses the production heuristic (`app/agent/lang_heuristic._classify`),
    which returns None whenever the signal is weak. A None is scored as
    inapplicable, NOT as a failure: the heuristic is deliberately silent on
    short or ambiguous text, and turning its silence into a red cell would
    make this metric mostly noise.
    """
    answer = _answer(result)
    if not answer.strip():
        return _verdict("language_match", 0.0, "empty answer")
    detected = _classify(answer)
    expected = str(case.get("lang") or "ru")
    if detected is None:
        return _verdict("language_match", 1.0, "no clear language signal in the answer",
                        applicable=False)
    ok = detected == expected
    return _verdict("language_match", 1.0 if ok else 0.0,
                    f"expected {expected}, detected {detected}")


def no_error(case: Mapping[str, Any], result: Result) -> dict[str, Any]:  # noqa: ARG001
    """Did the turn complete without raising? The cheapest possible smoke
    signal, and the one that tells a real regression from a broken harness."""
    error = result.get("error")
    if error:
        return _verdict("no_error", 0.0, str(error)[:300])
    return _verdict("no_error", 1.0, "ok")


# ---------------------------------------------------------------------------
# Suite assembly
# ---------------------------------------------------------------------------

def default_evaluators(
    *,
    latency_budget_ms: float = 8000.0,
    product_names: Iterable[str] = (),
    faq_texts: Iterable[str] = (),
) -> list[Callable[[Mapping[str, Any], Result], dict[str, Any]]]:
    """The suite the CLI runs. `reached_db` is bound to whatever reference
    data the caller could gather (live DB catalogue or offline fixtures)."""
    names = list(product_names)
    texts = list(faq_texts)

    def _reached_db(case: Mapping[str, Any], result: Result) -> dict[str, Any]:
        return reached_db(case, result, product_names=names, faq_texts=texts)

    _reached_db.__name__ = "reached_db"

    return [
        no_error,
        tool_match,
        category_match,
        _reached_db,
        no_pii_leak,
        language_match,
        latency_under(latency_budget_ms),
    ]


def evaluate_result(
    case: Mapping[str, Any],
    result: Result,
    evaluators: Optional[list] = None,
    **kwargs: Any,
) -> list[dict[str, Any]]:
    """Run a suite over one result, returning the list of verdicts."""
    suite = evaluators if evaluators is not None else default_evaluators(**kwargs)
    return [ev(case, result) for ev in suite]
