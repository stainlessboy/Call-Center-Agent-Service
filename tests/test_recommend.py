"""Tests for app.agent.recommend.rank_products — pure, deterministic
product-ranking heuristics (no LLM, no DB)."""
from __future__ import annotations

from app.agent.recommend import rank_products


def _product(name, rate, age_min=None, age_max=None, extra_rules=None):
    rules = []
    if age_min is not None or age_max is not None:
        rules.append({"age_min": age_min, "age_max": age_max, "rate_min_pct": rate})
    if extra_rules:
        rules.extend(extra_rules)
    return {"name": name, "rate_min_pct": rate, "rate_rules": rules}


class TestRankProductsBasic:
    def test_empty_products_returns_empty(self):
        assert rank_products([], None, "mortgage") == []

    def test_lower_rate_ranks_first_with_no_profile(self):
        cheap = _product("Cheap", 15.0)
        pricey = _product("Pricey", 20.0)
        ranked = rank_products([pricey, cheap], None, "mortgage")
        assert [p["name"] for p in ranked] == ["Cheap", "Pricey"]
        assert "best_rate" in ranked[0]["_recommend_reasons"]

    def test_top_n_limits_results(self):
        products = [_product(f"P{i}", 10.0 + i) for i in range(5)]
        ranked = rank_products(products, None, "mortgage", top_n=2)
        assert len(ranked) == 2

    def test_missing_rate_does_not_crash(self):
        no_rate = {"name": "NoRate", "rate_rules": []}
        ranked = rank_products([no_rate], None, "mortgage")
        assert len(ranked) == 1
        assert ranked[0]["_recommend_reasons"] == []


class TestRankProductsAgeFit:
    def test_age_within_bounds_gets_bonus_and_reason(self):
        young_only = _product("YoungOnly", 18.0, age_min=18, age_max=25)
        anyone = _product("Anyone", 19.0, age_min=None, age_max=None)
        profile = {"facts": {"age": 22}}
        ranked = rank_products([anyone, young_only], profile, "autoloan")
        assert ranked[0]["name"] == "YoungOnly"
        assert "age_fit" in ranked[0]["_recommend_reasons"]

    def test_age_outside_bounds_is_penalized_but_not_excluded(self):
        young_only = _product("YoungOnly", 18.0, age_min=18, age_max=25)
        profile = {"facts": {"age": 55}}
        ranked = rank_products([young_only], profile, "autoloan")
        # still returned (soft ranking signal, not a hard filter)
        assert len(ranked) == 1
        assert "age_fit" not in ranked[0]["_recommend_reasons"]

    def test_no_age_in_profile_is_neutral(self):
        young_only = _product("YoungOnly", 18.0, age_min=18, age_max=25)
        anyone = _product("Anyone", 18.0)
        ranked = rank_products([young_only, anyone], {"facts": {}}, "autoloan")
        # tie on rate, no age info to break it -> both present, order stable
        assert {p["name"] for p in ranked} == {"YoungOnly", "Anyone"}

    def test_invalid_age_in_facts_is_ignored(self):
        young_only = _product("YoungOnly", 18.0, age_min=18, age_max=25)
        profile = {"facts": {"age": "not-a-number"}}
        # must not raise
        ranked = rank_products([young_only], profile, "autoloan")
        assert len(ranked) == 1
        assert "age_fit" not in ranked[0]["_recommend_reasons"]


class TestRankProductsCombined:
    def test_age_fit_can_outrank_a_slightly_cheaper_product(self):
        cheap_wrong_age = _product("CheapWrongAge", 15.0, age_min=60, age_max=70)
        pricier_right_age = _product("PricierRightAge", 17.0, age_min=18, age_max=40)
        profile = {"facts": {"age": 25}}
        ranked = rank_products([cheap_wrong_age, pricier_right_age], profile, "mortgage")
        assert ranked[0]["name"] == "PricierRightAge"
