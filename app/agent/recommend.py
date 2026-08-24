"""Deterministic product-ranking engine for the `recommend_product` tool.

`rank_products` is a pure function — no LLM, no I/O, no i18n. It scores
products already scoped to ONE category (the shape `_get_products_by_category`
returns — see app/agent/products.py; every other part of this codebase, e.g.
qualify.py/calc_flow.py, likewise always operates on a category-scoped product
list, and that dict has no per-product "category" field to check against).

Consequently the "goal → category" correspondence heuristic mentioned in the
Phase 2 spec is resolved by the CALLER (`app/agent/tools.py::recommend_product`
picks the category from the client's goal text before fetching products), not
inside this function — there is nothing left to score once you're only
looking at one category's products. What actually varies product-to-product
WITHIN that category, and so is what `rank_products` scores, is:
  * whether the client's known age (from their profile) falls inside the
    product's rate-tier age bounds, and
  * the product's lowest advertised rate (ascending — lower is better).

Each returned product dict is annotated with `_recommend_reasons` (a list of
tags from a fixed vocabulary — `age_fit`, `best_rate` — for the CALLER to
turn into localized pitch text; this module never renders user-facing
strings) and `_recommend_score` (for debugging/tests, not shown to users).
"""
from __future__ import annotations

from typing import Any, Optional


def _coerce_age(facts: dict[str, Any]) -> Optional[int]:
    raw = facts.get("age")
    if raw is None:
        return None
    try:
        age = int(float(raw))
    except (TypeError, ValueError):
        return None
    return age if 0 < age < 130 else None


def _product_age_bounds(product: dict[str, Any]) -> tuple[Optional[int], Optional[int]]:
    """Union of age bounds across the product's rate tiers (rate_rules).

    Coarse by design: a real eligibility check would need to know which
    single tier applies (requires income_type/amount/term too — see
    app/agent/rate_rules.py::select_rate), which recommend_product doesn't
    collect. This only answers "is the client's age anywhere in range for
    ANY tier of this product", used as a soft ranking signal, not a hard
    eligibility filter — an out-of-range age lowers a product's rank, it
    does not remove it from the results.
    """
    rules = product.get("rate_rules") or []
    mins = [r["age_min"] for r in rules if r.get("age_min") is not None]
    maxs = [r["age_max"] for r in rules if r.get("age_max") is not None]
    return (min(mins) if mins else None, max(maxs) if maxs else None)


def rank_products(
    products: list[dict[str, Any]],
    profile: Optional[dict[str, Any]],
    goal_category: str,
    top_n: int = 2,
) -> list[dict[str, Any]]:
    """Rank *products* (all belonging to *goal_category*) for *profile*.

    Returns up to *top_n* products, best first, each with an added
    `_recommend_reasons: list[str]` (tags: `"age_fit"`, `"best_rate"`) and
    `_recommend_score: float`. Products with no usable rate are already
    filtered out upstream by `_get_products_by_category` (see
    `rate_rules.has_usable_rate`), so every product here has a `rate_min_pct`.

    Deterministic, side-effect-free — safe to unit test without a DB or LLM.
    """
    if not products:
        return []

    facts = (profile or {}).get("facts") or {}
    client_age = _coerce_age(facts)

    rates = [p["rate_min_pct"] for p in products if isinstance(p.get("rate_min_pct"), (int, float))]
    best_rate = min(rates) if rates else None

    scored: list[tuple[float, dict[str, Any], list[str]]] = []
    for product in products:
        score = 0.0
        reasons: list[str] = []

        rate = product.get("rate_min_pct")
        if isinstance(rate, (int, float)):
            # Ascending rate preference: lower rate scores higher. Scaled
            # down (rates are single-digit-to-double-digit percentages) so
            # this doesn't dominate the age-fit bonus below.
            score += (100.0 - float(rate)) / 10.0
            if best_rate is not None and abs(float(rate) - float(best_rate)) < 0.01:
                reasons.append("best_rate")

        if client_age is not None:
            age_lo, age_hi = _product_age_bounds(product)
            if age_lo is not None or age_hi is not None:
                in_range = (age_lo is None or client_age >= age_lo) and (
                    age_hi is None or client_age <= age_hi
                )
                if in_range:
                    score += 5.0
                    reasons.append("age_fit")
                else:
                    score -= 5.0

        scored.append((score, product, reasons))

    # Stable sort by score descending; ties keep the original (DB) order.
    scored.sort(key=lambda t: t[0], reverse=True)

    ranked: list[dict[str, Any]] = []
    for score, product, reasons in scored[:top_n]:
        annotated = dict(product)
        annotated["_recommend_reasons"] = reasons
        annotated["_recommend_score"] = score
        ranked.append(annotated)
    return ranked
