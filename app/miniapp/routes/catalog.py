"""Product catalog, product detail and the qualification decision trees."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.agent import qualify
from app.agent.i18n import at
from app.agent.products import _get_products_by_category
from app.db.models import User
from app.miniapp.deps import get_db_user, user_lang
from app.miniapp.serializers import (
    category_title,
    product_detail,
    product_id,
    product_summary,
)

router = APIRouter()

# Catalog grouping mirrors screen 09 in the design handoff.
CATALOG_GROUPS: list[dict[str, Any]] = [
    {
        "key": "credits",
        "categories": ["mortgage", "autoloan", "microloan", "education_credit"],
    },
    {
        "key": "savings",
        "categories": ["deposit", "debit_card", "fx_card"],
    },
]

GROUP_TITLES = {
    "credits": {"ru": "Кредиты", "en": "Loans", "uz": "Kreditlar"},
    "savings": {"ru": "Сбережения и карты", "en": "Savings & cards", "uz": "Jamg'arma va kartalar"},
}

CATEGORY_SUBTITLES = {
    "mortgage": {
        "ru": "Первичка, вторичка, ремонт",
        "en": "New builds, resale, renovation",
        "uz": "Birlamchi, ikkilamchi, ta'mir",
    },
    "autoloan": {
        "ru": "UzAuto и другие марки",
        "en": "UzAuto and other brands",
        "uz": "UzAuto va boshqa markalar",
    },
    "microloan": {
        "ru": "Без залога, онлайн или в ЦБУ",
        "en": "No collateral, online or in branch",
        "uz": "Garovsiz, onlayn yoki BXMda",
    },
    "education_credit": {
        "ru": "Оплата обучения",
        "en": "Tuition financing",
        "uz": "O'qish uchun to'lov",
    },
    "deposit": {
        "ru": "UZS, USD, EUR",
        "en": "UZS, USD, EUR",
        "uz": "UZS, USD, EUR",
    },
    "debit_card": {
        "ru": "Uzcard, Humo, Visa",
        "en": "Uzcard, Humo, Visa",
        "uz": "Uzcard, Humo, Visa",
    },
    "fx_card": {
        "ru": "Для оплаты за рубежом",
        "en": "For spending abroad",
        "uz": "Chet elda to'lovlar uchun",
    },
}

ALL_CATEGORIES = [c for g in CATALOG_GROUPS for c in g["categories"]]


def _localized(table: dict, key: str, lang: str) -> str:
    entry = table.get(key) or {}
    return entry.get(lang) or entry.get("ru") or ""


def _require_category(category: str) -> str:
    if category not in ALL_CATEGORIES:
        raise HTTPException(status_code=404, detail=f"Unknown category: {category}")
    return category


@router.get("/catalog")
async def catalog(user: User = Depends(get_db_user)) -> dict:
    lang = user_lang(user)
    return {
        "groups": [
            {
                "key": group["key"],
                "title": _localized(GROUP_TITLES, group["key"], lang),
                "categories": [
                    {
                        "category": category,
                        "title": category_title(category, lang),
                        "subtitle": _localized(CATEGORY_SUBTITLES, category, lang),
                        "has_qualify": category in qualify.QUALIFY_CATEGORIES,
                    }
                    for category in group["categories"]
                ],
            }
            for group in CATALOG_GROUPS
        ]
    }


@router.get("/products")
async def products(
    category: str,
    user: User = Depends(get_db_user),
) -> dict:
    _require_category(category)
    lang = user_lang(user)
    items = await _get_products_by_category(category)
    return {
        "category": category,
        "category_label": category_title(category, lang),
        "items": [product_summary(p, category, lang) for p in items],
    }


async def _find_product(category: str, pid: str) -> dict | None:
    for item in await _get_products_by_category(category):
        if product_id(category, str(item.get("name") or "")) == pid:
            return item
    return None


@router.get("/products/{pid}")
async def product(pid: str, user: User = Depends(get_db_user)) -> dict:
    category, _, _ = pid.rpartition("_")
    _require_category(category)
    found = await _find_product(category, pid)
    if found is None:
        raise HTTPException(status_code=404, detail="Product not found")
    return product_detail(found, category, user_lang(user))


# ---------------------------------------------------------------------------
# Qualification
# ---------------------------------------------------------------------------

@router.get("/qualify/{category}/tree")
async def qualify_tree(category: str, user: User = Depends(get_db_user)) -> dict:
    _require_category(category)
    tree = qualify.get_tree(category)
    if not tree:
        raise HTTPException(status_code=404, detail="No qualification tree for this category")
    lang = user_lang(user)

    nodes: dict[str, Any] = {}
    for key, node in tree["nodes"].items():
        node_type = node.get("type")
        payload: dict[str, Any] = {"key": key, "type": node_type}
        if node_type == qualify.NODE_QUESTION:
            payload["question"] = at(node["q"], lang)
            payload["options"] = [
                {
                    "index": i,
                    "label": at(opt["label"], lang),
                    "set": opt.get("set") or {},
                    "goto": opt.get("goto"),
                }
                for i, opt in enumerate(node.get("options") or [])
            ]
        elif node_type == qualify.NODE_DEAD_END:
            payload["message"] = at(node["message"], lang)
        nodes[key] = payload

    # Longest question chain from the entry node — drives the "Вопрос N из M"
    # progress bar without the client walking the tree itself.
    def depth(key: str, seen: frozenset[str]) -> int:
        node = tree["nodes"].get(key)
        if not node or node.get("type") != qualify.NODE_QUESTION or key in seen:
            return 0
        children = [o.get("goto") for o in (node.get("options") or []) if o.get("goto")]
        return 1 + max((depth(c, seen | {key}) for c in children), default=0)

    return {
        "category": category,
        "category_label": category_title(category, lang),
        "entry": tree["entry"],
        "max_steps": depth(tree["entry"], frozenset()),
        "nodes": nodes,
    }


class QualifyResultPayload(BaseModel):
    category: str
    answers: dict[str, Any] = Field(default_factory=dict)


@router.post("/qualify/result")
async def qualify_result(
    payload: QualifyResultPayload,
    user: User = Depends(get_db_user),
) -> dict:
    _require_category(payload.category)
    lang = user_lang(user)
    items = await qualify.filter_qualified_products(payload.category, payload.answers)
    serialized = [product_summary(p, payload.category, lang) for p in items]

    # "Лучшая ставка" badge (screen 12) — lowest rate wins; deposits invert.
    best_id = None
    if serialized:
        if payload.category == "deposit":
            with_rate = [s for s in serialized if s.get("rate_pct") is not None]
            if with_rate:
                best_id = max(with_rate, key=lambda s: s["rate_pct"])["id"]
        else:
            with_rate = [s for s in serialized if s.get("rate_min_pct") is not None]
            if with_rate:
                best_id = min(with_rate, key=lambda s: s["rate_min_pct"])["id"]

    return {
        "category": payload.category,
        "items": serialized,
        "best_id": best_id,
        "empty_message": at("qualify_no_offers", lang) if not serialized else "",
    }
