"""Dev fixture: populate the DB with 3 demo credit products per credit type.

NOT part of the Excel seed pipeline (/admin/seed). This is a standalone helper
for manually testing the normalized CreditProductOffer + CreditRateRule model
and the FLOW_QUALIFY / calculator flows without needing the real xlsx files.

Idempotent: every product it creates is marked source_path='demo'; re-running
deletes the previous demo products (rules cascade) and re-inserts them.
Manually-entered (non-demo) products and rules are left untouched.

Run:
    source .venv/bin/activate
    python3 scripts/seed_demo_credits.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import delete  # noqa: E402

from app.db.models import CreditProductOffer, CreditRateRule  # noqa: E402
from app.db.session import AsyncSessionLocal  # noqa: E402

DEMO_MARKER = "demo"


def _rule(rate, rate_max=None, *, income=None, age_min=None, age_max=None,
          amount_min=None, amount_max=None, term_min=None, term_max=None,
          dp_min=None, dp_max=None, currency=None, priority=0, cond=None):
    return CreditRateRule(
        income_type=income,
        age_min=age_min, age_max=age_max,
        amount_min=amount_min, amount_max=amount_max,
        term_min_months=term_min, term_max_months=term_max,
        downpayment_min_pct=dp_min, downpayment_max_pct=dp_max,
        currency_code=currency,
        rate_min_pct=float(rate),
        rate_max_pct=float(rate_max if rate_max is not None else rate),
        condition_text=cond,
        priority=priority,
        source=DEMO_MARKER,
        is_active=True,
    )


# (section, service_name, static kwargs, [rules]) -------------------------------
#
# Each credit TYPE showcases ONE clear rate dimension. We set only the relevant
# single bound per rule ("до 24 мес", "от 50% взноса", "до 30 млн") and let the
# engine pick the cheapest matching rule — that reproduces real bracket tables:
#   Ипотека        → ставка от первоначального взноса (downpayment)
#   Автокредит     → ставка от возраста (age)            ← здесь спрашивается возраст
#   Микрозайм      → ставка от срока (term)
#   Образовательный→ ставка от суммы кредита (amount)
M = 1_000_000

# One condition axis per credit type (matches the single-axis-per-product model).
_SECTION_KIND = {
    "Ипотека": "downpayment",
    "Автокредит": "age",
    "Микрозайм": "term",
    "Образовательный": "amount",
}

PRODUCTS: list[tuple[str, str, dict, list[CreditRateRule]]] = [
    # ----- Ипотека: ставка от первоначального взноса --------------------------
    ("Ипотека", "Ипотека на первичном рынке", dict(
        min_age=18, amount_min=50 * M, amount_max=1_500 * M,
        purpose_text="Покупка жилья в новостройке",
        collateral_text="Залог приобретаемого жилья",
        for_market_primary=True,
    ), [
        _rule(14, dp_min=50, cond="Взнос от 50% — 14% годовых"),
        _rule(16, dp_min=30, cond="Взнос от 30% — 16% годовых"),
        _rule(18, dp_min=15, cond="Взнос от 15% — 18% годовых"),
    ]),
    ("Ипотека", "Ипотека на вторичном рынке", dict(
        min_age=18, amount_min=50 * M, amount_max=1_200 * M,
        purpose_text="Покупка готового жилья",
        collateral_text="Залог приобретаемого жилья",
        for_market_secondary=True,
    ), [
        _rule(16, dp_min=40, cond="Взнос от 40% — 16% годовых"),
        _rule(19, dp_min=20, cond="Взнос от 20% — 19% годовых"),
    ]),
    ("Ипотека", "Кредит на ремонт жилья", dict(
        min_age=18, amount_min=10 * M, amount_max=300 * M,
        purpose_text="Ремонт и благоустройство жилья",
        collateral_text="Залог недвижимости",
        for_renovation=True,
    ), [
        _rule(20, dp_min=30, cond="Взнос от 30% — 20% годовых"),
        _rule(23, dp_min=10, cond="Взнос от 10% — 23% годовых"),
    ]),

    # ----- Автокредит: ставка от возраста (спрашивается возраст) --------------
    ("Автокредит", "Автокредит на авто марки GM", dict(
        min_age=18, amount_min=30 * M, amount_max=500 * M,
        purpose_text="Покупка автомобиля Chevrolet/UzAuto",
        collateral_text="Залог приобретаемого автомобиля",
        for_brand_gm=True,
    ), [
        _rule(18, age_max=30, cond="До 30 лет — 18% годовых"),
        _rule(20, age_max=45, cond="До 45 лет — 20% годовых"),
        _rule(23, age_max=65, cond="До 65 лет — 23% годовых"),
    ]),
    ("Автокредит", "Автокредит на иномарки", dict(
        min_age=21, amount_min=50 * M, amount_max=800 * M,
        purpose_text="Покупка автомобиля иностранного бренда",
        collateral_text="Залог приобретаемого автомобиля",
        for_brand_other=True,
    ), [
        _rule(22, age_max=35, cond="До 35 лет — 22% годовых"),
        _rule(26, age_max=65, cond="До 65 лет — 26% годовых"),
    ]),
    ("Автокредит", "Автокредит «Молодёжный» (GM)", dict(
        min_age=18, amount_min=30 * M, amount_max=300 * M,
        purpose_text="Покупка авто GM, льготы по возрасту",
        collateral_text="Залог приобретаемого автомобиля",
        for_brand_gm=True,
    ), [
        _rule(17, age_max=25, cond="До 25 лет — 17% годовых"),
        _rule(19, age_max=35, cond="До 35 лет — 19% годовых"),
    ]),

    # ----- Микрозайм: ставка от срока -----------------------------------------
    ("Микрозайм", "Микрозайм онлайн", dict(
        min_age=18, amount_min=1 * M, amount_max=50 * M,
        purpose_text="Потребительские нужды, оформление онлайн",
        collateral_text="Без обеспечения",
        channel_online=True,
    ), [
        _rule(27, term_max=24, cond="До 24 месяцев — 27% годовых"),
        _rule(29, term_max=48, cond="До 48 месяцев — 29% годовых"),
        _rule(32, term_max=60, cond="До 60 месяцев — 32% годовых"),
    ]),
    ("Микрозайм", "Микрозайм в ЦБУ", dict(
        min_age=18, amount_min=1 * M, amount_max=70 * M,
        purpose_text="Потребительские нужды, оформление в отделении",
        collateral_text="Без обеспечения",
        channel_cbu=True,
    ), [
        _rule(25, term_max=24, cond="До 24 месяцев — 25% годовых"),
        _rule(27, term_max=48, cond="До 48 месяцев — 27% годовых"),
        _rule(30, term_max=60, cond="До 60 месяцев — 30% годовых"),
    ]),
    ("Микрозайм", "Микрозайм «Экспресс» онлайн", dict(
        min_age=18, amount_min=1 * M, amount_max=20 * M,
        purpose_text="Срочный заём онлайн",
        collateral_text="Без обеспечения",
        channel_online=True,
    ), [
        _rule(30, term_max=12, cond="До 12 месяцев — 30% годовых"),
        _rule(33, term_max=24, cond="До 24 месяцев — 33% годовых"),
    ]),

    # ----- Образовательный: ставка от суммы кредита ---------------------------
    ("Образовательный", "Образовательный кредит", dict(
        min_age=18, amount_min=5 * M, amount_max=150 * M,
        purpose_text="Оплата обучения",
        collateral_text="Без обеспечения",
    ), [
        _rule(12, amount_max=30 * M, cond="До 30 млн — 12% годовых"),
        _rule(14, amount_max=70 * M, cond="До 70 млн — 14% годовых"),
        _rule(16, amount_max=150 * M, cond="До 150 млн — 16% годовых"),
    ]),
    ("Образовательный", "Образовательный кредит (магистратура)", dict(
        min_age=20, amount_min=10 * M, amount_max=150 * M,
        purpose_text="Оплата магистратуры",
        collateral_text="Без обеспечения",
    ), [
        _rule(13, amount_max=50 * M, cond="До 50 млн — 13% годовых"),
        _rule(15, amount_max=150 * M, cond="До 150 млн — 15% годовых"),
    ]),
    ("Образовательный", "Образовательный кредит онлайн", dict(
        min_age=18, amount_min=5 * M, amount_max=80 * M,
        purpose_text="Оплата обучения, оформление онлайн",
        collateral_text="Без обеспечения",
    ), [
        _rule(11, amount_max=40 * M, cond="До 40 млн — 11% годовых"),
        _rule(13, amount_max=80 * M, cond="До 80 млн — 13% годовых"),
    ]),
]


async def main() -> None:
    async with AsyncSessionLocal() as session:
        # Idempotency: wipe previous demo products (rules cascade via FK).
        await session.execute(
            delete(CreditProductOffer).where(CreditProductOffer.source_path == DEMO_MARKER)
        )
        await session.flush()

        n_products = 0
        n_rules = 0
        for section, service_name, static, rules in PRODUCTS:
            product = CreditProductOffer(
                section_name=section,
                service_name=service_name,
                source_path=DEMO_MARKER,
                rate_condition_kind=_SECTION_KIND.get(section, "flat"),
                is_active=True,
                **static,
            )
            product.rate_rules = rules
            session.add(product)
            n_products += 1
            n_rules += len(rules)

        await session.commit()

    by_section: dict[str, int] = {}
    for section, *_ in PRODUCTS:
        by_section[section] = by_section.get(section, 0) + 1
    print(f"✅ Загружено {n_products} демо-продуктов и {n_rules} тарифов.")
    for section, cnt in by_section.items():
        print(f"   • {section}: {cnt}")
    print("Источник всех записей: source_path='demo' (повторный запуск перезальёт их).")


if __name__ == "__main__":
    asyncio.run(main())
