"""Rate-rule matching engine for credit products.

A credit product owns a list of rate rules (loaded as dicts by
``app/utils/data_loaders.py``). Each rule is a condition tier:

    {income_type, age_min/max, amount_min/max, term_min/max_months,
     downpayment_min/max_pct, currency_code, rate_min_pct, rate_max_pct,
     condition_text, priority}

Any NULL bound means that axis is unconstrained. ``select_rate`` picks the best
rule for a set of user inputs; a rule that constrains an axis only matches when
the corresponding input is present and inside the range — so age-dependent rules
require the caller to supply ``age`` (see ``needs_age``).
"""
from __future__ import annotations

from typing import Any, Optional

# (min_key, max_key, input_key) for the numeric range axes a rule may constrain.
_RANGE_AXES = (
    ("age_min", "age_max", "age"),
    ("amount_min", "amount_max", "amount"),
    ("term_min_months", "term_max_months", "term_months"),
    ("downpayment_min_pct", "downpayment_max_pct", "downpayment_pct"),
)

# Exact-match axes.
_EXACT_AXES = (
    ("income_type", "income_type"),
    ("currency_code", "currency_code"),
)


def _in_range(value: Optional[float], lo: Optional[float], hi: Optional[float]) -> bool:
    if value is None:
        return False
    if lo is not None and value < lo:
        return False
    if hi is not None and value > hi:
        return False
    return True


def _rule_rate(rule: dict[str, Any]) -> Optional[float]:
    """The rate a matched rule yields (lower bound of its range)."""
    return rule.get("rate_min_pct")


def has_usable_rate(rules: Optional[list[dict[str, Any]]]) -> bool:
    """True if any rule carries a concrete rate — used to gate product visibility
    (products with no usable rate are hidden from the bot)."""
    return any(_rule_rate(r) is not None for r in (rules or []))


def needs_age(rules: Optional[list[dict[str, Any]]]) -> bool:
    """True if any rule's rate depends on the applicant's age."""
    return any(
        r.get("age_min") is not None or r.get("age_max") is not None
        for r in (rules or [])
    )


def rate_bounds(
    rules: Optional[list[dict[str, Any]]],
) -> tuple[Optional[float], Optional[float]]:
    """Min/max rate across all usable rules — for the 'ставка от X%' display."""
    lows: list[float] = []
    highs: list[float] = []
    for r in rules or []:
        lo = r.get("rate_min_pct")
        if lo is None:
            continue
        lows.append(float(lo))
        hi = r.get("rate_max_pct")
        highs.append(float(hi) if hi is not None else float(lo))
    if not lows:
        return None, None
    return min(lows), max(highs)


def _match_specificity(rule: dict[str, Any], inputs: dict[str, Any]) -> Optional[int]:
    """Return how many constrained axes the rule matched, or None if it fails any
    axis it constrains."""
    specificity = 0
    for lo_k, hi_k, in_k in _RANGE_AXES:
        lo, hi = rule.get(lo_k), rule.get(hi_k)
        if lo is None and hi is None:
            continue  # unconstrained axis
        if not _in_range(inputs.get(in_k), lo, hi):
            return None
        specificity += 1
    for rule_k, in_k in _EXACT_AXES:
        want = rule.get(rule_k)
        if want is None:
            continue
        if inputs.get(in_k) != want:
            return None
        specificity += 1
    return specificity


def select_rate(
    rules: Optional[list[dict[str, Any]]],
    *,
    age: Optional[int] = None,
    amount: Optional[float] = None,
    term_months: Optional[int] = None,
    downpayment_pct: Optional[float] = None,
    income_type: Optional[str] = None,
    currency_code: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    """Pick the best matching rule for the given inputs.

    Ranking: highest ``priority`` → most specific (most constrained axes
    matched) → lowest rate. Returns the rule dict, or None if nothing matches.
    """
    inputs = {
        "age": age,
        "amount": amount,
        "term_months": term_months,
        "downpayment_pct": downpayment_pct,
        "income_type": income_type,
        "currency_code": currency_code,
    }
    best: Optional[tuple[int, int, float, dict[str, Any]]] = None
    for rule in rules or []:
        rate = _rule_rate(rule)
        if rate is None:
            continue
        spec = _match_specificity(rule, inputs)
        if spec is None:
            continue
        # (priority, specificity, -rate): higher is better on all three.
        key = (int(rule.get("priority", 0)), spec, -float(rate))
        if best is None or key > best[:3]:
            best = (*key, rule)
    return best[3] if best is not None else None


def select_rate_value(
    rules: Optional[list[dict[str, Any]]], **inputs: Any
) -> Optional[float]:
    """Convenience wrapper returning just the matched rate (or None)."""
    rule = select_rate(rules, **inputs)
    return _rule_rate(rule) if rule is not None else None
