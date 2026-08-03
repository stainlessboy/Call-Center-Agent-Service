"""Offices: list, search, nearest-by-geolocation, detail and the service matrix."""
from __future__ import annotations

from math import asin, cos, radians, sin, sqrt

from fastapi import APIRouter, Depends, HTTPException

from app.agent.branches import (
    ALL_OFFICE_TYPES,
    SERVICE_CODES,
    get_office_type_label,
    office_types_for_service,
    search_offices,
)
from app.db.models import User
from app.miniapp.deps import get_chat_service, get_db_user, user_lang
from app.miniapp.serializers import office as serialize_office
from app.services.chat_service import ChatService

router = APIRouter()

SERVICE_LABELS: dict[str, dict[str, str]] = {
    "credit_individual": {
        "ru": "Кредиты физлицам",
        "en": "Retail loans",
        "uz": "Jismoniy shaxslarga kreditlar",
    },
    "autoloan": {"ru": "Автокредит", "en": "Auto loan", "uz": "Avtokredit"},
    "credit_legal": {"ru": "Услуги ИП и юрлиц", "en": "Business services", "uz": "YaTT va yuridik shaxslar"},
    "atm": {"ru": "Банкомат", "en": "ATM", "uz": "Bankomat"},
    "consultation": {"ru": "Консультации", "en": "Consultation", "uz": "Maslahat"},
    "non_credit_ops": {
        "ru": "Некредитные операции",
        "en": "Non-credit operations",
        "uz": "Kreditsiz operatsiyalar",
    },
    "cards": {"ru": "Пластиковые карты", "en": "Cards", "uz": "Plastik kartalar"},
    "cashier": {"ru": "Касса и обмен валют", "en": "Cash desk & exchange", "uz": "Kassa va valyuta ayirboshlash"},
}

# Order shown in the comparison matrix (screen 22).
SERVICE_ORDER = [
    "credit_individual",
    "autoloan",
    "credit_legal",
    "cards",
    "non_credit_ops",
    "cashier",
    "atm",
    "consultation",
]


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    dlat = radians(lat2 - lat1)
    dlon = radians(lon2 - lon1)
    a = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2) ** 2
    return 2 * r * asin(sqrt(a))


@router.get("/branches")
async def list_branches(
    type: str | None = None,
    q: str = "",
    limit: int = 20,
    user: User = Depends(get_db_user),
) -> dict:
    if type and type not in ALL_OFFICE_TYPES:
        raise HTTPException(status_code=422, detail=f"Unknown office type: {type}")
    lang = user_lang(user)
    items = await search_offices(
        query=q.strip(),
        office_types=[type] if type else None,
        limit=max(1, min(limit, 100)),
    )
    return {
        "items": [serialize_office(o, lang) for o in items],
        "types": [
            {"code": code, "label": get_office_type_label(code, lang)}
            for code in ALL_OFFICE_TYPES
        ],
    }


@router.get("/branches/nearest")
async def nearest_branches(
    lat: float,
    lon: float,
    limit: int = 5,
    user: User = Depends(get_db_user),
    chat_service: ChatService = Depends(get_chat_service),
) -> dict:
    lang = user_lang(user)
    offices = await chat_service.list_all_offices_with_coords()
    scored = []
    for obj in offices:
        obj_lat = getattr(obj, "latitude", None)
        obj_lon = getattr(obj, "longitude", None)
        if obj_lat is None or obj_lon is None:
            continue
        scored.append((_haversine_km(lat, lon, float(obj_lat), float(obj_lon)), obj))
    scored.sort(key=lambda pair: pair[0])
    return {
        "items": [
            serialize_office(obj, lang, distance_km=distance)
            for distance, obj in scored[: max(1, min(limit, 20))]
        ]
    }


@router.get("/branches/service-matrix")
async def service_matrix(user: User = Depends(get_db_user)) -> dict:
    lang = user_lang(user)
    available_by_service = {
        code: set(office_types_for_service(code)) for code in SERVICE_CODES
    }
    return {
        "types": [
            {"code": code, "label": get_office_type_label(code, lang)}
            for code in ALL_OFFICE_TYPES
        ],
        "services": [
            {
                "code": code,
                "label": (SERVICE_LABELS.get(code) or {}).get(lang)
                or (SERVICE_LABELS.get(code) or {}).get("ru")
                or code,
                "availability": {
                    office_type: office_type in available_by_service.get(code, set())
                    for office_type in ALL_OFFICE_TYPES
                },
            }
            for code in SERVICE_ORDER
        ],
    }


@router.get("/branches/{office_type}/{office_id}")
async def branch_detail(
    office_type: str,
    office_id: int,
    user: User = Depends(get_db_user),
    chat_service: ChatService = Depends(get_chat_service),
) -> dict:
    if office_type not in ALL_OFFICE_TYPES:
        raise HTTPException(status_code=422, detail=f"Unknown office type: {office_type}")
    obj = await chat_service.get_office_by_id(office_type, office_id)
    if obj is None:
        raise HTTPException(status_code=404, detail="Office not found")
    lang = user_lang(user)
    payload = serialize_office(obj, lang)
    payload["services"] = [
        {
            "code": code,
            "label": (SERVICE_LABELS.get(code) or {}).get(lang)
            or (SERVICE_LABELS.get(code) or {}).get("ru")
            or code,
        }
        for code in SERVICE_ORDER
        if office_type in office_types_for_service(code)
    ]
    return payload
