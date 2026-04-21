"""Branch / office type logic: service matrix + polymorphic DB search.

There are three DB models — Filial, SalesOffice, SalesPoint — each in its
own table. This module exposes a unified search that returns a mixed list
of them, plus a service matrix mapping office-type codes to the services
offered there (codified from the bank's regulation — see screenshot
"Разница во функционале офисов").
"""
from __future__ import annotations

from typing import List, Union

from sqlalchemy import or_, select

from app.db.models import Filial, SalesOffice, SalesPoint
from app.db.session import get_session

# ---------------------------------------------------------------------------
# Office type codes (strings). Each model also exposes OFFICE_TYPE_CODE.
# ---------------------------------------------------------------------------

FILIAL = Filial.OFFICE_TYPE_CODE          # "filial"
SALES_OFFICE = SalesOffice.OFFICE_TYPE_CODE  # "sales_office"
SALES_POINT = SalesPoint.OFFICE_TYPE_CODE    # "sales_point"

ALL_OFFICE_TYPES: tuple[str, ...] = (FILIAL, SALES_OFFICE, SALES_POINT)

OfficeObj = Union[Filial, SalesOffice, SalesPoint]

_MODEL_BY_TYPE: dict[str, type] = {
    FILIAL: Filial,
    SALES_OFFICE: SalesOffice,
    SALES_POINT: SalesPoint,
}


# ---------------------------------------------------------------------------
# Service matrix — what each office type can actually do
# ---------------------------------------------------------------------------

SERVICE_CODES = {
    "credit_individual",  # потреб/авто/микро/образ
    "autoloan",           # автокредит (единственный кредит у точки продаж)
    "credit_legal",       # услуги ИП и юрлиц
    "atm",                # устройства самообслуживания
    "consultation",       # консультации и инфо
    "non_credit_ops",     # некредитные операции физлиц (кроме вкладов)
    "cards",              # пластиковые карты
    "cashier",            # касса + обмен валют
}

_SERVICE_MATRIX: dict[str, set[str]] = {
    FILIAL: {
        "credit_individual", "autoloan", "credit_legal",
        "atm", "consultation", "non_credit_ops", "cards", "cashier",
    },
    SALES_OFFICE: {
        "credit_individual", "autoloan",
        "atm", "consultation", "non_credit_ops", "cards", "cashier",
    },
    SALES_POINT: {
        "autoloan", "atm", "consultation",
    },
}


def office_types_for_service(service: str) -> list[str]:
    """Return list of office-type codes where the given service is available."""
    service = (service or "").strip().lower()
    if service not in SERVICE_CODES:
        # Unknown service → every office type
        return list(ALL_OFFICE_TYPES)
    return [ot for ot, services in _SERVICE_MATRIX.items() if service in services]


def get_office_type_label(office_type: str, lang: str = "ru") -> str:
    labels = {
        FILIAL: {"ru": "Филиал (ЦБУ)", "uz": "Filial (BXM)", "en": "Branch"},
        SALES_OFFICE: {"ru": "Офис продаж (мини-офис)", "uz": "Savdo ofisi (mini-ofis)", "en": "Sales office"},
        SALES_POINT: {"ru": "Точка продаж (автосалон)", "uz": "Savdo nuqtasi (avtosalon)", "en": "Sales point (car dealer)"},
    }
    return labels[office_type].get(lang) or labels[office_type]["ru"]


# ---------------------------------------------------------------------------
# Polymorphic search
# ---------------------------------------------------------------------------

def _build_query(model: type, query: str):
    """Build a SELECT with ILIKE across name/address/region (when present)."""
    stmt = select(model)
    q = (query or "").strip()
    if not q:
        return stmt
    pattern = f"%{q}%"
    conds = [
        model.name_ru.ilike(pattern),
        model.name_uz.ilike(pattern),
        model.address_ru.ilike(pattern),
        model.address_uz.ilike(pattern),
    ]
    if hasattr(model, "region_ru"):
        conds.append(model.region_ru.ilike(pattern))
        conds.append(model.region_uz.ilike(pattern))
    return stmt.where(or_(*conds))


async def search_offices(
    query: str = "",
    office_types: list[str] | None = None,
    limit: int = 5,
) -> List[OfficeObj]:
    """Search across filials, sales_offices, sales_points and return a unified list.

    Args:
        query: free-form text — city, region, branch name.
        office_types: list of type codes to include (None → all).
        limit: max total results returned (split proportionally by type).
    """
    types = list(office_types) if office_types else list(ALL_OFFICE_TYPES)
    results: List[OfficeObj] = []

    async with get_session() as session:
        for code in types:
            model = _MODEL_BY_TYPE.get(code)
            if model is None:
                continue
            stmt = _build_query(model, query).order_by(model.name_ru).limit(limit)
            result = await session.execute(stmt)
            results.extend(result.scalars().all())

    # Stable ordering: filials first, then offices, then points; then by name
    _order = {FILIAL: 0, SALES_OFFICE: 1, SALES_POINT: 2}
    results.sort(key=lambda obj: (_order.get(obj.OFFICE_TYPE_CODE, 9), getattr(obj, "name_ru", "") or ""))
    return results[:limit]


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

def _localized(obj: OfficeObj, field: str, lang: str) -> str | None:
    """Pick `{field}_uz` for lang=uz if present, else `{field}_ru`."""
    if lang == "uz":
        val = getattr(obj, f"{field}_uz", None)
        if val:
            return val
    return getattr(obj, f"{field}_ru", None)


def format_branch_card(obj: OfficeObj, lang: str = "ru") -> str:
    """Format a single office as a short user-facing string."""
    type_label = get_office_type_label(obj.OFFICE_TYPE_CODE, lang)
    name = _localized(obj, "name", lang) or ""
    address = _localized(obj, "address", lang) or ""
    landmark = _localized(obj, "landmark", lang) if hasattr(obj, "landmark_ru") else None
    location_url = getattr(obj, "location_url", None)

    lines = [f"<b>{name}</b> — {type_label}"]
    if address:
        lines.append(f"📍 {address}")
    if landmark:
        lines.append(f"🧭 {landmark}")
    if location_url:
        map_label = {"ru": "Карта", "uz": "Xaritada", "en": "Map"}.get(lang, "Карта")
        lines.append(f"🗺 <a href=\"{location_url}\">{map_label}</a>")
    if getattr(obj, "phone", None):
        lines.append(f"📞 {obj.phone}")
    if getattr(obj, "hours", None):
        lines.append(f"🕒 {obj.hours}")
    return "\n".join(lines)


def format_branches_list(objs: List[OfficeObj], lang: str = "ru") -> str:
    if not objs:
        return ""
    return "\n\n".join(format_branch_card(o, lang) for o in objs)
