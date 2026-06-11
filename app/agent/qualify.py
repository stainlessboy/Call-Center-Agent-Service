"""FLOW_QUALIFY — pre-listing qualification questionnaire + product filtering.

Before showing products for a category, the bot asks a short, *branching* set of
questions (see the business decision trees). The answers map to DB filters, and
the matching products are pulled from the database — product names are NOT
hardcoded here. The trees below are pure data: changing which questions are asked
(or which answer leads where) is a config edit, not a code change.

How answers map to filters
--------------------------
- Salary / salary-card / self-employed  → ``income_types`` (subset of
  payroll / official / no_official). A product row matches if its ``income_type``
  is in the set OR is NULL (NULL = "applies to any income type").
- Auto brand / mortgage market / microloan channel → a boolean ``tag`` column on
  ``CreditProductOffer`` (set manually in SQLAdmin). A product (``service_name``)
  matches if ANY of its rows carries the flag — so tagging one row is enough.
- Deposit goal / currency → ``topup_allowed`` / ``partial_withdrawal_allowed`` /
  ``currency_code`` on ``DepositProductOffer`` (already populated by the seeder).

A node is either a ``question`` (with options) or a terminal (``filter`` runs the
DB filter and shows products; ``dead_end`` shows a fixed message).

Note on data population: the boolean tag columns are filled manually in SQLAdmin
(seeding rework is planned separately). Until products are tagged, brand/market/
channel branches will return an empty list, which the node surfaces as the
"no offers" message.
"""
from __future__ import annotations

import re
from collections import defaultdict
from typing import Any, Optional

from app.agent.i18n import at

# Terminal node types.
NODE_QUESTION = "question"
NODE_FILTER = "filter"
NODE_DEAD_END = "dead_end"


# ---------------------------------------------------------------------------
# Decision trees (data)
# ---------------------------------------------------------------------------
# Each tree: {"entry": <node_key>, "nodes": {<key>: <node>}}.
# Question node: {"type": "question", "q": <i18n_key>, "options": [<option>]}.
#   option: {"label": <i18n_btn_key>, "match": (<free-text tokens>),
#            "set": {<answer updates>}, "goto": <next node key>}.
# Terminal nodes: {"type": "filter"} | {"type": "dead_end", "message": <i18n_key>}.

_YES_TOKENS = ("да", "ha", "yes", "bor", "есть")
_NO_TOKENS = ("нет", "yo'q", "yoq", "no", "yq")

_TREES: dict[str, dict[str, Any]] = {
    # ── Autoloan ─────────────────────────────────────────────────────────
    "autoloan": {
        "entry": "salary",
        "nodes": {
            "salary": {
                "type": NODE_QUESTION,
                "q": "q_salary_autoloan",
                "options": [
                    {"label": "btn_q_yes", "match": _YES_TOKENS + ("официальн", "белая зарплата", "rasmiy"),
                     "goto": "salary_card"},
                    {"label": "btn_q_no", "match": _NO_TOKENS + ("без официальн", "неофициальн", "rasmiy emas"),
                     "goto": "self_employed"},
                ],
            },
            "salary_card": {
                "type": NODE_QUESTION,
                "q": "q_salary_card",
                "options": [
                    {"label": "btn_asaka", "match": ("асака", "asaka", "ваш банк", "вашего банка"),
                     "set": {"income_types": ["payroll"]}, "goto": "auto_brand"},
                    {"label": "btn_other_bank", "match": ("друг", "boshqa", "other", "иного банка"),
                     "set": {"income_types": ["official"]}, "goto": "auto_brand"},
                ],
            },
            "self_employed": {
                "type": NODE_QUESTION,
                "q": "q_self_employed",
                "options": [
                    {"label": "btn_q_yes", "match": _YES_TOKENS + ("самозанят", "self employed", "yakka tartib", "tadbirkor"),
                     "set": {"income_types": ["no_official"]}, "goto": "auto_brand"},
                    {"label": "btn_q_no", "match": _NO_TOKENS, "goto": "dead_no_offers"},
                ],
            },
            "auto_brand": {
                "type": NODE_QUESTION,
                "q": "q_auto_brand",
                "options": [
                    {"label": "btn_brand_gm", "match": ("gm", "uzauto", "chevrolet", "damas", "cobalt", "onix", "tracker", "malibu", "spark", "nexia"),
                     "set": {"tag": "for_brand_gm"}, "goto": "result"},
                    {"label": "btn_brand_other", "match": ("иная", "иной", "друг", "boshqa", "other", "kia", "hyundai", "byd", "toyota", "chery", "changan"),
                     "set": {"tag": "for_brand_other"}, "goto": "result"},
                ],
            },
            "result": {"type": NODE_FILTER},
            "dead_no_offers": {"type": NODE_DEAD_END, "message": "qualify_no_offers"},
        },
    },
    # ── Mortgage ─────────────────────────────────────────────────────────
    "mortgage": {
        "entry": "salary",
        "nodes": {
            "salary": {
                "type": NODE_QUESTION,
                "q": "q_salary_mortgage",
                "options": [
                    {"label": "btn_q_yes", "match": _YES_TOKENS + ("официальн", "rasmiy"),
                     "set": {"income_types": ["payroll", "official"]}, "goto": "market"},
                    {"label": "btn_q_no", "match": _NO_TOKENS + ("без официальн", "неофициальн", "rasmiy emas"),
                     "goto": "self_employed"},
                ],
            },
            "self_employed": {
                "type": NODE_QUESTION,
                "q": "q_self_employed",
                "options": [
                    {"label": "btn_q_yes", "match": _YES_TOKENS + ("самозанят", "self employed", "yakka tartib", "tadbirkor"),
                     "set": {"income_types": ["no_official"]}, "goto": "market"},
                    {"label": "btn_q_no", "match": _NO_TOKENS, "goto": "dead_no_offers"},
                ],
            },
            "market": {
                "type": NODE_QUESTION,
                "q": "q_mortgage_market",
                "options": [
                    {"label": "btn_market_primary", "match": ("первичн", "новострой", "от застройщика", "birlamchi", "primary", "new build"),
                     "set": {"tag": "for_market_primary"}, "goto": "result"},
                    {"label": "btn_market_secondary", "match": ("вторичн", "ikkilamchi", "secondary"),
                     "set": {"tag": "for_market_secondary"}, "goto": "result"},
                    {"label": "btn_renovation", "match": ("ремонт", "ta'mir", "tamir", "renovation", "repair"),
                     "set": {"tag": "for_renovation"}, "goto": "result"},
                ],
            },
            "result": {"type": NODE_FILTER},
            "dead_no_offers": {"type": NODE_DEAD_END, "message": "qualify_no_offers"},
        },
    },
    # ── Microloan ────────────────────────────────────────────────────────
    "microloan": {
        "entry": "salary",
        "nodes": {
            "salary": {
                "type": NODE_QUESTION,
                "q": "q_salary_microloan",
                "options": [
                    {"label": "btn_q_yes", "match": _YES_TOKENS + ("официальн", "rasmiy"),
                     "goto": "salary_card"},
                    {"label": "btn_q_no", "match": _NO_TOKENS + ("без официальн", "неофициальн", "rasmiy emas"),
                     "goto": "self_employed"},
                ],
            },
            "salary_card": {
                "type": NODE_QUESTION,
                "q": "q_salary_card",
                "options": [
                    {"label": "btn_asaka", "match": ("асака", "asaka", "ваш банк"),
                     "set": {"income_types": ["payroll"]}, "goto": "channel"},
                    {"label": "btn_other_bank", "match": ("друг", "boshqa", "other"),
                     "set": {"income_types": ["official"]}, "goto": "channel"},
                ],
            },
            "self_employed": {
                "type": NODE_QUESTION,
                "q": "q_self_employed",
                "options": [
                    {"label": "btn_q_yes", "match": _YES_TOKENS + ("самозанят", "self employed", "yakka tartib", "tadbirkor"),
                     "set": {"income_types": ["no_official"]}, "goto": "result"},
                    {"label": "btn_q_no", "match": _NO_TOKENS, "goto": "dead_consider_others"},
                ],
            },
            "channel": {
                "type": NODE_QUESTION,
                "q": "q_microloan_channel",
                "options": [
                    {"label": "btn_channel_cbu", "match": ("цбу", "офис", "отделени", "filial", "bo'lim", "boʻlim"),
                     "set": {"tag": "channel_cbu"}, "goto": "result"},
                    {"label": "btn_channel_online", "match": ("онлайн", "online", "ilova", "приложени", "masofadan"),
                     "set": {"tag": "channel_online"}, "goto": "result"},
                ],
            },
            "result": {"type": NODE_FILTER},
            "dead_consider_others": {"type": NODE_DEAD_END, "message": "qualify_consider_others"},
        },
    },
    # ── Deposit ──────────────────────────────────────────────────────────
    "deposit": {
        "entry": "goal",
        "nodes": {
            "goal": {
                "type": NODE_QUESTION,
                "q": "q_deposit_goal",
                "options": [
                    {"label": "btn_deposit_topup", "match": ("копить", "накоп", "накапл", "jamg'ar", "jamgar", "save", "to'plash"),
                     "set": {"deposit_goal": "topup"}, "goto": "currency"},
                    {"label": "btn_deposit_withdraw", "match": ("снимать", "снят", "частичн", "yechib", "withdraw"),
                     "set": {"deposit_goal": "withdrawal"}, "goto": "currency"},
                ],
            },
            "currency": {
                "type": NODE_QUESTION,
                "q": "q_deposit_currency",
                "options": [
                    {"label": "btn_currency_uzs", "match": ("сум", "so'm", "som", "uzs"),
                     "set": {"deposit_currency": "UZS"}, "goto": "result"},
                    {"label": "btn_currency_usd", "match": ("доллар", "usd", "dollar", "$"),
                     "set": {"deposit_currency": "USD"}, "goto": "result"},
                    {"label": "btn_currency_eur", "match": ("евро", "eur", "yevro", "€"),
                     "set": {"deposit_currency": "EUR"}, "goto": "result"},
                ],
            },
            "result": {"type": NODE_FILTER},
        },
    },
}

# Placeholder tree for categories whose business questions are not defined yet
# (education_credit, debit_card, fx_card). One stub question, then show all
# products in the category. Replace with a real tree later — config-only change.
_PLACEHOLDER_TREE: dict[str, Any] = {
    "entry": "stub",
    "nodes": {
        "stub": {
            "type": NODE_QUESTION,
            "q": "q_placeholder",
            "options": [
                {"label": "btn_continue", "match": ("продолж", "davom", "continue", "ok", "ок", "да"),
                 "goto": "result"},
            ],
        },
        "result": {"type": NODE_FILTER},
    },
}

_PLACEHOLDER_CATEGORIES = ("education_credit", "debit_card", "fx_card")

# All categories that go through the qualification flow.
QUALIFY_CATEGORIES = tuple(_TREES.keys()) + _PLACEHOLDER_CATEGORIES


def get_tree(category: str) -> Optional[dict[str, Any]]:
    """Return the decision tree for *category*, or None if it has no flow."""
    if category in _TREES:
        return _TREES[category]
    if category in _PLACEHOLDER_CATEGORIES:
        return _PLACEHOLDER_TREE
    return None


def get_node(category: str, node_key: str) -> Optional[dict[str, Any]]:
    tree = get_tree(category)
    if not tree:
        return None
    return tree["nodes"].get(node_key)


def entry_node_key(category: str) -> Optional[str]:
    tree = get_tree(category)
    return tree["entry"] if tree else None


# ---------------------------------------------------------------------------
# Answer matching
# ---------------------------------------------------------------------------

_NORM_RE = re.compile(r"[^\w\s'\-/]", re.UNICODE)


def _norm(s: str) -> str:
    """Lowercase, drop emoji/punctuation, collapse whitespace (keeps Cyrillic)."""
    s = (s or "").lower().strip()
    s = _NORM_RE.sub("", s)
    return re.sub(r"\s+", " ", s).strip()


def match_answer(node: dict[str, Any], user_text: str, lang: str = "ru") -> Optional[dict[str, Any]]:
    """Match *user_text* to one of *node*'s options.

    Matching order: numeric index → exact localized label (any language) →
    free-text token match (short tokens require whole-word, longer ones
    substring). Returns the matched option dict or None.
    """
    options = node.get("options") or []
    norm = _norm(user_text)
    if not norm or not options:
        return None

    # 1. Numeric index ("2" → second option, 1-based).
    if norm.isdigit():
        idx = int(norm) - 1
        if 0 <= idx < len(options):
            return options[idx]

    # 2. Exact match against any localized button label.
    for opt in options:
        for lng in ("ru", "en", "uz"):
            if _norm(at(opt["label"], lng)) == norm:
                return opt

    # 3. Free-text token match.
    words = set(norm.split())
    for opt in options:
        for tok in opt.get("match", ()):
            t = _norm(tok)
            if not t:
                continue
            if len(t) <= 3:
                if t in words:
                    return opt
            elif t in norm:
                return opt
    return None


def render_buttons(node: dict[str, Any], lang: str) -> list[str]:
    return [at(opt["label"], lang) for opt in node.get("options") or []]


def prefill(category: str, text: str, lang: str = "ru") -> tuple[Optional[str], dict[str, Any]]:
    """Pre-answer questions from a single free-text message ("if they say it all
    up front, skip the questions").

    Walks the tree from the entry node, matching the message against each
    question's options and following the branch. Stops at the first question it
    cannot answer (returns that node key) or at a terminal (returns its key).
    Returns (node_key, accumulated_answers).
    """
    tree = get_tree(category)
    if not tree:
        return None, {}
    answers: dict[str, Any] = {}
    node_key = tree["entry"]
    seen: set[str] = set()
    while node_key and node_key not in seen:
        seen.add(node_key)
        node = tree["nodes"].get(node_key)
        if not node or node.get("type") != NODE_QUESTION:
            return node_key, answers  # reached a terminal
        opt = match_answer(node, text, lang)
        if not opt:
            return node_key, answers  # stop — ask this question
        answers.update(opt.get("set") or {})
        node_key = opt.get("goto")
    return node_key, answers


# ---------------------------------------------------------------------------
# Product filtering (terminal "filter" nodes)
# ---------------------------------------------------------------------------

async def filter_qualified_products(category: str, answers: dict[str, Any]) -> list[dict]:
    """Return products matching the collected *answers* for *category*."""
    if category == "deposit":
        return await _filter_deposits(answers)
    if category in ("debit_card", "fx_card"):
        from app.agent.products import _get_products_by_category
        return await _get_products_by_category(category)
    return await _filter_credits(category, answers)


async def _filter_credits(category: str, answers: dict[str, Any]) -> list[dict]:
    from app.agent.constants import CREDIT_SECTION_MAP
    from app.agent.products import _get_products_by_category
    from app.utils.data_loaders import _load_credit_product_offers

    section = CREDIT_SECTION_MAP.get(category)
    if not section:
        return await _get_products_by_category(category)

    income_types = answers.get("income_types")
    tag = answers.get("tag")

    allowed: set[str] = set()
    for product in await _load_credit_product_offers():
        if product.get("section_name") != section:
            continue
        name = str(product.get("service_name") or "").strip()
        if not name:
            continue
        # Tags now live on the product row (one row per product).
        tag_ok = (tag is None) or (product.get(tag) is True)
        # income_type lives on the rate rules; a NULL income_type (or a product
        # with no rules) matches any income branch.
        rule_incomes = [r.get("income_type") for r in (product.get("rate_rules") or [])]
        income_ok = (
            (not income_types)
            or (not rule_incomes)
            or any((it in income_types or it is None) for it in rule_incomes)
        )
        if income_ok and tag_ok:
            allowed.add(name)

    products = await _get_products_by_category(category)
    return [p for p in products if p.get("name") in allowed]


async def _filter_deposits(answers: dict[str, Any]) -> list[dict]:
    from app.agent.products import _get_products_by_category
    from app.utils.data_loaders import _load_deposit_product_offers

    goal = answers.get("deposit_goal")
    currency = answers.get("deposit_currency")

    by_name: dict[str, list[dict]] = defaultdict(list)
    for row in await _load_deposit_product_offers():
        name = str(row.get("service_name") or "").strip()
        if name:
            by_name[name].append(row)

    allowed: set[str] = set()
    for name, rows in by_name.items():
        cur_ok = (not currency) or any(r.get("currency_code") == currency for r in rows)
        if goal == "topup":
            goal_ok = any(r.get("topup_allowed") is True for r in rows)
        elif goal == "withdrawal":
            goal_ok = any(r.get("partial_withdrawal_allowed") is True for r in rows)
        else:
            goal_ok = True
        if cur_ok and goal_ok:
            allowed.add(name)

    products = await _get_products_by_category("deposit")
    return [p for p in products if p.get("name") in allowed]
