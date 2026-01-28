from typing import Any, Dict, List, Optional
import yaml
import re
import json


# =========================
#  Загрузка продуктов
# =========================

def load_products_from_yaml(path: str) -> List[Dict[str, Any]]:
    """
    Загружает перечень продуктов из YAML-файла.
    Ожидается структура:
      products:
        - id: ...
          category: "mortgage"
          ...
    """
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return data["products"]


# =========================
#  Хелперы по профилю клиента
# =========================

def _age_ok_for_product(product: Dict[str, Any], profile: Dict[str, Any]) -> bool:
    """
    Проверяет возраст клиента для конкретного продукта:
      - age_min / age_max (общие),
      - age_max_male / age_max_female (по полу).

    В profile:
      age: int
      gender: "male" / "female" / None
    """
    elig = product.get("eligibility") or {}
    age = profile.get("age")

    if age is None:
        return True

    age_min = elig.get("age_min")
    age_max = elig.get("age_max")

    if age_min is not None and age < age_min:
        return False
    if age_max is not None and age > age_max:
        return False

    gender = profile.get("gender")  # "male" / "female"
    max_male = elig.get("age_max_male")
    max_female = elig.get("age_max_female")

    if gender == "male" and max_male is not None and age > max_male:
        return False
    if gender == "female" and max_female is not None and age > max_female:
        return False

    # Если пол не задан, а есть гендерные ограничения — берём самый строгий максимум
    if gender is None and (max_male is not None or max_female is not None):
        max_vals = [v for v in (max_male, max_female) if v is not None]
        if max_vals and age > min(max_vals):
            return False

    return True


# =========================
#  Program (standard / bi_group / nrg_2_4 / daho / ...)
# =========================

def _mortgage_program_ok(product: Dict[str, Any], profile: Dict[str, Any]) -> bool:
    """
    Роутинг по программе ипотеки.

    В YAML у продукта:
      mortgage_program: "standard" / "bi_group" / "nrg_2_4" / "daho" / ...

    В profile:
      mortgage_program: "standard" / "bi_group" / "nrg_2_4" / "daho" / None

    Логика:
      - если профиль указал конкретную программу → берём только совпадающие продукты;
      - если профиль не указал (None) → не фильтруем.
    """
    prod_prog = product.get("mortgage_program", "standard")
    req_prog = profile.get("mortgage_program")

    if req_prog is None:
        return True
    return prod_prog == req_prog


# =========================
#  Purpose (segment + keys)
# =========================

def _purpose_segment_ok(product: Dict[str, Any], profile: Dict[str, Any]) -> bool:
    """
    purpose_segment: "housing" / "consumer" / "business" / "any"
    """
    prod_seg = product.get("purpose_segment", "any")
    req_seg = profile.get("purpose_segment")

    if req_seg is None:
        return True
    if prod_seg == "any":
        return True
    if req_seg == "any":
        return True
    return prod_seg == req_seg


def _purpose_keys_ok(product: Dict[str, Any], profile: Dict[str, Any]) -> bool:
    """
    purposes: список ключей продукта, например ['housing_primary', 'housing_repair'].
    profile['purpose_keys']: список ключей, которые выбрал клиент.
    """
    req_keys = profile.get("purpose_keys")
    if not req_keys:
        return True

    prod_keys = product.get("purposes") or []
    req_set = set(req_keys)
    prod_set = set(prod_keys)

    return len(req_set & prod_set) > 0


def _purpose_ok(product: Dict[str, Any], profile: Dict[str, Any]) -> bool:
    """
    Продукт подходит по цели, если:
      - совпал сегмент (housing/consumer/business/any),
      - есть пересечение purpose-ключей.
    """
    return _purpose_segment_ok(product, profile) and _purpose_keys_ok(product, profile)


# =========================
#  Региональные лимиты
# =========================

def _get_region_limit(
    product: Dict[str, Any],
    purpose_key: str,
    region_code: str,
) -> Optional[int]:
    """
    Максимальный лимит по сумме для комбинации (регион, цель).
    region_amount_limits:
      tashkent:
        housing_primary: 800000000
      regions:
        housing_primary: 500000000
      any:
        housing_repair: 170000000
    """
    limits = product.get("region_amount_limits") or {}

    # 1) Конкретный регион
    if region_code in limits:
        region_block = limits[region_code] or []
        if purpose_key in region_block:
            return int(region_block[purpose_key])

    # 2) Общий блок "any"
    if "any" in limits:
        any_block = limits["any"] or []
        if purpose_key in any_block:
            return int(any_block[purpose_key])

    return None


def _region_amount_ok(product: Dict[str, Any], profile: Dict[str, Any], amount: int) -> bool:
    """
    Проверяем, что сумма не превышает региональные лимиты для выбранной цели.
    Если region_code или purpose_keys нет — проверку мягко пропускаем.
    """
    limits = product.get("region_amount_limits") or {}
    if not limits:
        return True

    region_code = profile.get("region_code")  # "tashkent" / "regions" / др.
    if not region_code:
        return True

    purpose_keys = profile.get("purpose_keys") or []
    if not purpose_keys:
        return True

    max_allowed: Optional[int] = None

    for key in purpose_keys:
        limit = _get_region_limit(product, key, region_code)
        if limit is None:
            continue
        if max_allowed is None or limit > max_allowed:
            max_allowed = limit

    # Если лимита нет ни по одной цели — не режем
    if max_allowed is None:
        return True

    return amount <= max_allowed


# =========================
#  Первоначальный взнос
# =========================

def _compute_required_downpayment_pct(product: Dict[str, Any], profile: Dict[str, Any]) -> Optional[float]:
    """
    Определяет минимальный процент первоначального взноса для КОНКРЕТНОГО профиля.

    Логика:
      1) Если у продукта есть downpayment_rules:
           - when: "income_proof"   → min_pct = X
           - when: "!income_proof"  → min_pct = Y
         то возвращается соответствующее значение в зависимости от того,
         есть ли у клиента официальный доход (income_proof = True/False).
      2) Если downpayment_rules нет, но есть downpayment_min_pct,
         то возвращаем его.
      3) Если ничего нет — возвращаем None.
    """
    rules = product.get("downpayment_rules") or []
    has_official = bool(profile.get("income_proof"))

    if rules:
        selected: Optional[float] = None
        for r in rules:
            cond = r.get("when")
            min_pct = r.get("min_pct")
            if min_pct is None:
                continue

            if cond == "income_proof" and has_official:
                selected = float(min_pct)
            elif cond == "!income_proof" and not has_official:
                selected = float(min_pct)
            elif cond is None and selected is None:
                selected = float(min_pct)

        if selected is not None:
            return selected

    # Фолбэк — простой минимум
    min_dp = product.get("downpayment_min_pct")
    if min_dp is not None:
        return float(min_dp)

    return None


def _format_sums(amount: Optional[int]) -> str:
    """
    Удобное форматирование сумм в сумах: 900000000 → '900 000 000'.
    """
    if amount is None:
        return "—"
    return f"{amount:,}".replace(",", " ")


# =========================
#  Eligibility (ипотека)
# =========================

def _eligibility_ok_mortgage(product: Dict[str, Any], profile: Dict[str, Any]) -> bool:
    """
    Eligibility для ипотеки:
      - гражданство (citizen_uz_required),
      - ТОЛЬКО официальный доход (income_proof = True), если official_income_required = True,
      - возраст (через _age_ok_for_product).

    Для спец-продуктов (BI Group, NRG, DAHO), где может допускаться неофициальный доход,
    в YAML ставим official_income_required: false.
    """
    elig = product.get("eligibility") or {}

    # Гражданство
    if elig.get("citizen_uz_required") and not profile.get("citizen_uz", False):
        return False

    # Официальный доход обязателен, если official_income_required = True
    if elig.get("official_income_required"):
        if not profile.get("income_proof", False):
            return False

    # Возраст
    if not _age_ok_for_product(product, profile):
        return False

    return True


# =========================
#  APR (годовая ставка)
# =========================

_cond_pattern = re.compile(r"^(amount|term)\s*(<=|>=|==|<|>)\s*([0-9]+)$")


def _eval_when(condition: Optional[str],
               profile: Dict[str, Any],
               amount: int,
               term: int) -> bool:
    """
    Интерпретатор строки condition из apr_rules.

    Поддерживает:
      - логическое И: "a && b && c"
      - флаг: "income_proof"
      - сравнения: "amount<=3000000", "term>24", "term==13"
    """
    if not condition:
        return True

    has_official_income = bool(profile.get("income_proof"))
    parts = [p.strip() for p in condition.split("&&") if p.strip()]

    for part in parts:
        # флаг официального дохода
        if part == "income_proof":
            if not has_official_income:
                return False
            continue

        # сравнения amount/term
        m = _cond_pattern.match(part)
        if not m:
            return False

        field, op, value_str = m.groups()
        value = int(value_str)
        current = amount if field == "amount" else term

        if op == "<=" and not (current <= value):
            return False
        if op == ">=" and not (current >= value):
            return False
        if op == "<" and not (current < value):
            return False
        if op == ">" and not (current > value):
            return False
        if op == "==" and not (current == value):
            return False

    return True


def get_mortgage_apr(
    product: Dict[str, Any],
    profile: Dict[str, Any],
) -> Optional[float]:
    """
    Находит подходящее правило из product['apr_rules'].

    - Для большинства ипотек хватает when: null (фиксированная ставка).
    - Срок может быть НЕ указан клиентом → тогда берём
      технический term = term_max_months или 240, чтобы _eval_when не упал.

    Для DAHO реальные комбинации ставок смотрим в daho_config.offers, а
    этот APR используем только для грубой сортировки продуктов.
    """
    amount_src = profile.get("requested_amount")
    if amount_src is None:
        raise ValueError("requested_amount обязателен для расчёта APR по ипотеке")

    term_src = profile.get("requested_term_months")
    tmax = product.get("term_max_months")
    if term_src is None:
        term_src = tmax if tmax is not None else 240

    amount = int(amount_src)
    term = int(term_src)

    rules = product.get("apr_rules") or []
    applicable: List[float] = []

    for rule in rules:
        cond = rule.get("when")
        apr = rule["apr"]
        if _eval_when(cond, profile, amount, term):
            applicable.append(float(apr))

    if not applicable:
        return None
    return min(applicable)


# =========================
#  BI/NRG 2.4: детальное описание двух линий
# =========================

def describe_bi_group_structure(
    product: Dict[str, Any],
    profile: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """
    Формирует детальное описание структуры кредита для продуктов с
    двухлинейной схемой 2.4 (BI Group, NRG и т.п.).

    Требования к продукту:
      - есть блок product['bi_group_config'] с line_a / line_b / bxm_current;
      - profile['requested_amount'] — это СУММА КРЕДИТА
        (стоимость квартиры минус первоначальный взнос).
    """
    cfg = product.get("bi_group_config") or {}
    if not cfg:
        return None

    line_a_cfg = (cfg.get("line_a") or {}).copy()
    line_b_cfg = (cfg.get("line_b") or {}).copy()
    bxm = int(cfg.get("bxm_current") or 0)

    if not line_a_cfg or not line_b_cfg or bxm <= 0:
        return None

    amount_src = profile.get("requested_amount")
    if amount_src is None:
        return None

    amount = int(amount_src)
    product_name = product.get("name", "данный продукт")

    # Линия A — максимум 420 млн (из конфига)
    line_a_cap = int(line_a_cfg.get("max_amount") or 0)

    # Линия B — максимум 4000 * BXM
    max_mult = int(line_b_cfg.get("max_multiple_bxm") or 0)
    line_b_cap = max_mult * bxm if max_mult > 0 else 0

    has_official = bool(profile.get("income_proof"))
    required_dp_pct = _compute_required_downpayment_pct(product, profile)

    # Сценарий 1: одна линия A
    if amount <= line_a_cap:
        line_a_amount = amount
        line_b_amount = 0
        scenario = "single_line_a"

        dp_part = ""
        if required_dp_pct is not None:
            if has_official:
                dp_part = (
                    f"Так как у вас есть официальный доход, минимальный первоначальный взнос "
                    f"по этому продукту составляет {required_dp_pct:.0f}% от стоимости квартиры.\n\n"
                )
            else:
                dp_part = (
                    f"Так как у вас нет подтверждённого официального дохода, минимальный "
                    f"первоначальный взнос по этому продукту составляет {required_dp_pct:.0f}% "
                    f"от стоимости квартиры.\n\n"
                )

        explanation_ru = (
            f"По продукту «{product_name}» в вашем случае открывается одна кредитная линия — линия A, "
            f"так как сумма кредита { _format_sums(amount) } сумов не превышает 420 000 000 сумов.\n\n"
            f"{dp_part}"
            "Линия A открывается за счёт ресурсов Министерства экономики и финансов:\n"
            f"- сумма по линии A: { _format_sums(line_a_amount) } сумов;\n"
            f"- максимальный срок кредита по линии A: до { line_a_cfg.get('term_max_months') } месяцев;\n"
            f"- годовая ставка по линии A до получения кадастрового паспорта: "
            f"{ line_a_cfg.get('apr_before_cadastre') }%;\n"
            f"- годовая ставка по линии A после получения кадастрового паспорта: "
            f"{ line_a_cfg.get('apr_after_cadastre') }%.\n\n"
            "Линия B в этом сценарии не используется, так как вся сумма кредита укладывается "
            "в лимит 420 000 000 сумов по линии A."
        )

    # Сценарий 2: две линии A + B
    else:
        line_a_amount = line_a_cap
        line_b_amount = amount - line_a_cap
        if line_b_amount > line_b_cap:
            # Теоретически сюда не попадём из-за предварительного фильтра,
            # но на всякий случай ограничим.
            line_b_amount = line_b_cap

        scenario = "two_lines_a_and_b"

        dp_part = ""
        if required_dp_pct is not None:
            if has_official:
                dp_part = (
                    f"Так как у вас есть официальный доход, минимальный первоначальный взнос "
                    f"по этому продукту составляет {required_dp_pct:.0f}% от стоимости квартиры.\n\n"
                )
            else:
                dp_part = (
                    f"Так как у вас нет подтверждённого официального дохода, минимальный "
                    f"первоначальный взнос по этому продукту составляет {required_dp_pct:.0f}% "
                    f"от стоимости квартиры.\n\n"
                )

        explanation_ru = (
            f"По продукту «{product_name}» в вашем случае открываются две кредитные линии, "
            f"так как сумма кредита { _format_sums(amount) } сумов превышает 420 000 000 сумов.\n\n"
            f"{dp_part}"
            "Разделение происходит следующим образом:\n"
            f"- Линия A (ресурсы Министерства экономики и финансов): "
            f"{ _format_sums(line_a_amount) } сумов;\n"
            f"- Линия B (собственные ресурсы Асакабанка): "
            f"{ _format_sums(line_b_amount) } сумов.\n\n"
            "Условия по линии A:\n"
            f"- максимальный срок кредита по линии A: до { line_a_cfg.get('term_max_months') } месяцев;\n"
            f"- годовая ставка по линии A до получения кадастрового паспорта: "
            f"{ line_a_cfg.get('apr_before_cadastre') }%;\n"
            f"- годовая ставка по линии A после получения кадастрового паспорта: "
            f"{ line_a_cfg.get('apr_after_cadastre') }%.\n\n"
            "Условия по линии B:\n"
            f"- максимальный срок кредита по линии B: до { line_b_cfg.get('term_max_months') } месяцев;\n"
            f"- годовая ставка по линии B до получения кадастрового паспорта: "
            f"{ line_b_cfg.get('apr_before_cadastre') }%;\n"
            f"- годовая ставка по линии B после получения кадастрового паспорта: "
            f"{ line_b_cfg.get('apr_after_cadastre') }%.\n\n"
            "Таким образом, часть кредита финансируется за счёт ресурсов Министерства "
            "экономики и финансов по линии A, а превышение над 420 000 000 сумов покрывается "
            "по линии B за счёт собственных ресурсов Асакабанка в пределах лимита "
            "4000-кратного базового расчётного показателя (BXM)."
        )

    details: Dict[str, Any] = {
        "product_id": product.get("id"),
        "scenario": scenario,
        "requested_amount": amount,
        "required_downpayment_pct": required_dp_pct,
        "has_official_income": has_official,
        "line_a": {
            "amount": line_a_amount,
            "term_max_months": line_a_cfg.get("term_max_months"),
            "apr_before_cadastre": line_a_cfg.get("apr_before_cadastre"),
            "apr_after_cadastre": line_a_cfg.get("apr_after_cadastre"),
            "resource_name": line_a_cfg.get("resource_name"),
        },
        "line_b": {
            "amount": line_b_amount,
            "term_max_months": line_b_cfg.get("term_max_months"),
            "apr_before_cadastre": line_b_cfg.get("apr_before_cadastre"),
            "apr_after_cadastre": line_b_cfg.get("apr_after_cadastre"),
            "resource_name": line_b_cfg.get("resource_name"),
            "max_multiple_bxm": line_b_cfg.get("max_multiple_bxm"),
            "bxm_current": bxm,
        } if line_b_amount > 0 else None,
        "explanation_ru": explanation_ru,
    }

    return details


# =========================
#  DAHO: детальное описание фиксированных комбинаций
# =========================

def describe_daho_structure(
    product: Dict[str, Any],
    profile: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """
    Формирует детальное описание продукта «Ипотека DAHO» (или других продуктов
    с блоком daho_config) для ЛЛМ, чтобы модель НЕ придумывала условия сама.
    """
    cfg = product.get("daho_config") or {}
    if not cfg:
        return None

    product_name = product.get("name", "данный продукт")
    max_ltv = cfg.get("max_ltv_pct")
    offers = cfg.get("offers") or []
    coll_req = cfg.get("collateral_requirements") or {}

    has_official = bool(profile.get("income_proof"))
    is_self_employed = bool(profile.get("self_employed"))
    dp_pct = profile.get("downpayment_pct")  # может быть None
    term_pref = profile.get("requested_term_months")  # может быть None

    # Составим список текстовых строк по комбинациям срок/взнос/ставка
    offer_lines: List[str] = []
    available_terms: List[int] = []

    for off in offers:
        term_m = off.get("term_months")
        min_dp = off.get("min_downpayment_pct")
        apr = off.get("apr")

        if term_m is None or min_dp is None or apr is None:
            continue

        status_parts: List[str] = []

        # Подходит ли по внесённому взносу
        if dp_pct is not None and float(dp_pct) >= float(min_dp):
            status_parts.append("ваш первоначальный взнос позволяет выбрать этот срок")

        # Соответствует ли желаемому сроку
        if term_pref is not None and int(term_pref) == int(term_m):
            status_parts.append("этот срок соответствует вашему запросу по сроку")

        status = ""
        if status_parts:
            status = " (" + "; ".join(status_parts) + ")"

        line = (
            f"- срок до {term_m} месяцев, минимальный первоначальный взнос "
            f"{min_dp:.0f}% от стоимости квартиры — ставка {apr:.0f}% годовых{status}"
        )
        offer_lines.append(line)

        # Сохраним сроки, которые точно доступны при текущем dp_pct
        if dp_pct is not None and float(dp_pct) >= float(min_dp):
            available_terms.append(int(term_m))

    offers_text = "\n".join(offer_lines) if offer_lines else ""

    explanation_parts: List[str] = []

    # Общая логика по DAHO
    explanation_parts.append(
        f"По продукту «{product_name}» кредит выдаётся только на приобретение квартир "
        f"в строящихся жилых комплексах проекта DAHO, реализуемых в партнёрстве с банком."
    )

    if max_ltv is not None:
        explanation_parts.append(
            f"Максимальный размер кредита составляет не более {max_ltv:.0f}% "
            f"от стоимости квартиры по договору с подрядной организацией-партнёром. "
            f"Оплата за квартиру производится безналичным переводом подрядчику."
        )

    # Комментарий про то, что клиент пока не сказал, какой взнос / срок
    if dp_pct is None:
        explanation_parts.append(
            "Вы пока не указали размер первоначального взноса. Ниже перечислены все "
            "стандартные комбинации сроков и минимальных первоначальных взносов; "
            "подходящий вариант будет выбран при оформлении заявки исходя из того, "
            "какой взнос вы реально внесёте."
        )
    else:
        explanation_parts.append(
            f"Вы указали, что планируете первоначальный взнос около {dp_pct:.1f}% "
            "от стоимости квартиры. Ниже показано, при каких сроках такой взнос "
            "достаточен для стандартных условий продукта."
        )

    if term_pref is None:
        explanation_parts.append(
            "Вы пока не указали желаемый срок кредита, поэтому ниже приведены все "
            "доступные варианты сроков. При оформлении заявки вы сможете выбрать "
            "один из этих вариантов."
        )
    else:
        explanation_parts.append(
            f"Вы указали желаемый срок кредита {int(term_pref)} месяцев. Ниже показано, "
            "какие стандартные комбинации по продукту доступны и какие условия "
            "различаются по срокам."
        )

    if offers_text:
        explanation_parts.append(
            "По этому продукту предусмотрены фиксированные сочетания срока кредита, "
            "минимального первоначального взноса и годовой процентной ставки:\n"
            + offers_text
        )

    # Логика для самозанятых
    if is_self_employed and dp_pct is not None and float(dp_pct) >= 40.0:
        explanation_parts.append(
            "Так как вы зарегистрированы как самозанятое лицо и планируете внести "
            f"не менее 40% стоимости квартиры (фактически {dp_pct:.1f}%), "
            "подтверждение официального дохода по условиям продукта не требуется."
        )
    else:
        explanation_parts.append(
            "Для клиентов, зарегистрированных как самозанятые, при формировании "
            "первоначального взноса в размере 40% и более подтверждение официального "
            "дохода по этому продукту не требуется."
        )

    # Логика по кредитной истории при 50% взносе
    if dp_pct is not None and float(dp_pct) >= 50.0:
        explanation_parts.append(
            "Так как ваш первоначальный взнос составляет 50% стоимости квартиры или более, "
            "по условиям продукта кредитная история заемщика и совместного заемщика "
            "не проверяется (остальные проверки банка всё равно сохраняются)."
        )
    else:
        explanation_parts.append(
            "Если первоначальный взнос по ипотеке составляет 50% и более, "
            "кредитная история заемщика и совместного заемщика по этому продукту "
            "не проверяется."
        )

    # Обеспечение
    normal_coll = coll_req.get("normal_client_min_collateral_pct")
    related_coll = coll_req.get("related_client_min_collateral_pct")
    if normal_coll or related_coll:
        base = (
            "В качестве обеспечения принимается приобретаемое жилое помещение; "
            "до завершения строительства оформляется страховой полис или другое имущество. "
        )
        extra_parts: List[str] = []
        if normal_coll:
            extra_parts.append(
                f"для обычных клиентов размер обеспечения должен составлять "
                f"не менее {normal_coll:.0f}% суммы кредита"
            )
        if related_coll:
            extra_parts.append(
                f"для лиц, связанных с банком, размер обеспечения должен составлять "
                f"не менее {related_coll:.0f}% суммы кредита"
            )
        explanation_parts.append(
            base + " ".join(extra_parts) + "."
        )

    explanation_ru = "\n\n".join(explanation_parts)

    details: Dict[str, Any] = {
        "product_id": product.get("id"),
        "has_official_income": has_official,
        "is_self_employed": is_self_employed,
        "downpayment_pct": dp_pct,
        "max_ltv_pct": max_ltv,
        "offers": offers,
        "collateral_requirements": coll_req,
        "available_terms_for_client": available_terms if available_terms else None,
        "explanation_ru": explanation_ru,
    }

    return details


# =========================
#  Near-miss: причины, почему продукт не проходит
# =========================

def _collect_miss_reasons_mortgage(
    product: Dict[str, Any],
    profile: Dict[str, Any],
    amount: int,
    term: Optional[int],
) -> List[str]:
    """
    Возвращает список reason-кодов, по которым клиент НЕ подходит под продукт,
    если цель уже совпала.
    """
    reasons: List[str] = []

    # 1) Глобальные лимиты по сумме продукта
    amin = product.get("amount_min")
    amax = product.get("amount_max")
    if amin is not None and amount < amin:
        reasons.append("amount_below_min")
    if amax is not None and amount > amax:
        reasons.append("amount_above_product_max")

    # 2) Региональные лимиты
    if not _region_amount_ok(product, profile, amount):
        reasons.append("amount_above_region_limit")

    # 3) Срок
    tmin = product.get("term_min_months")
    tmax = product.get("term_max_months")
    if term is not None:
        if tmin is not None and term < tmin:
            reasons.append("term_below_min")
        if tmax is not None and term > tmax:
            reasons.append("term_above_max")

    # 4) Eligibility
    elig = product.get("eligibility") or {}

    if elig.get("citizen_uz_required") and not profile.get("citizen_uz", False):
        reasons.append("citizenship_required")

    if elig.get("official_income_required") and not profile.get("income_proof", False):
        reasons.append("official_income_required")

    # возраст
    age = profile.get("age")
    gender = profile.get("gender")
    age_min = elig.get("age_min")
    age_max_common = elig.get("age_max")
    max_male = elig.get("age_max_male")
    max_female = elig.get("age_max_female")

    if age is not None:
        if age_min is not None and age < age_min:
            reasons.append("age_below_min")
        if age_max_common is not None and age > age_max_common:
            reasons.append("age_above_max")
        if gender == "male" and max_male is not None and age > max_male:
            reasons.append("age_above_male_max")
        if gender == "female" and max_female is not None and age > max_female:
            reasons.append("age_above_female_max")
        if gender is None and (max_male is not None or max_female is not None):
            max_vals = [v for v in (max_male, max_female) if v is not None]
            if max_vals and age > min(max_vals):
                reasons.append("age_above_max_for_gender_unspecified")

    # 5) Первоначальный взнос (для DAHO не режем)
    if product.get("mortgage_program") != "daho":
        dp = profile.get("downpayment_pct")
        if dp is not None:
            required_min = _compute_required_downpayment_pct(product, profile)
            if required_min is not None and float(dp) < float(required_min):
                reasons.append("downpayment_below_required_min")

    return reasons


def _build_near_miss_reason_text_mortgage(
    product: Dict[str, Any],
    profile: Dict[str, Any],
    reason_codes: List[str],
) -> str:
    """
    Человеко-понятное объяснение, ПОЧЕМУ клиент не подходит под продукт,
    если цель совпала.
    """
    name = product.get("name", product.get("id", "этот продукт"))
    amount = profile.get("requested_amount")
    term = profile.get("requested_term_months")
    dp = profile.get("downpayment_pct")
    region_code = profile.get("region_code")
    purpose_keys = profile.get("purpose_keys") or []

    lines: List[str] = []
    lines.append(f"Продукт «{name}» подходит по цели, но сейчас вы не укладываетесь в его условия:")

    # Сумма
    if "amount_below_min" in reason_codes:
        amin = product.get("amount_min")
        if amin is not None and amount is not None:
            lines.append(
                f"- запрошенная сумма {_format_sums(amount)} сум ниже минимальной суммы по продукту "
                f"({_format_sums(amin)} сум)."
            )
        else:
            lines.append("- запрошенная сумма ниже минимальной суммы по продукту.")

    if "amount_above_product_max" in reason_codes:
        amax = product.get("amount_max")
        if amax is not None and amount is not None:
            lines.append(
                f"- запрошенная сумма {_format_sums(amount)} сум выше максимально допустимой суммы "
                f"по продукту ({_format_sums(amax)} сум)."
            )
        else:
            lines.append("- запрошенная сумма выше максимально допустимой суммы по продукту.")

    if "amount_above_region_limit" in reason_codes:
        max_limit: Optional[int] = None
        if region_code and purpose_keys:
            for key in purpose_keys:
                lim = _get_region_limit(product, key, region_code)
                if lim is not None and (max_limit is None or lim > max_limit):
                    max_limit = lim
        if max_limit is not None and amount is not None:
            lines.append(
                f"- для вашего региона и цели максимальная сумма по этому продукту "
                f"составляет {_format_sums(max_limit)} сум, а вы запросили "
                f"{_format_sums(amount)} сум."
            )
        else:
            lines.append("- сумма превышает региональный лимит по этому продукту.")

    # Срок
    if "term_below_min" in reason_codes or "term_above_max" in reason_codes:
        tmin = product.get("term_min_months")
        tmax = product.get("term_max_months")
        if term is not None:
            if "term_below_min" in reason_codes and tmin is not None:
                lines.append(
                    f"- желаемый срок {term} месяцев меньше минимального срока по продукту "
                    f"({tmin} месяцев)."
                )
            if "term_above_max" in reason_codes and tmax is not None:
                lines.append(
                    f"- желаемый срок {term} месяцев больше максимального срока по продукту "
                    f"({tmax} месяцев)."
                )
        else:
            lines.append(
                "- по сроку кредита есть ограничения, под которые ваш запрос сейчас не подпадает."
            )

    # Гражданство / доход
    if "citizenship_required" in reason_codes:
        lines.append(
            "- данный продукт предоставляется только гражданам Республики Узбекистан."
        )
    if "official_income_required" in reason_codes:
        lines.append(
            "- по данному продукту требуется подтверждённый официальный доход, "
            "а сейчас он не указан или не подтверждён."
        )

    # Возраст
    if "age_below_min" in reason_codes:
        lines.append("- ваш возраст ниже минимально допустимого возраста по этому продукту.")
    if any(c in reason_codes for c in (
        "age_above_max",
        "age_above_male_max",
        "age_above_female_max",
        "age_above_max_for_gender_unspecified",
    )):
        lines.append("- ваш возраст превышает максимальный допустимый возраст по условиям продукта.")

    # Взнос
    if "downpayment_below_required_min" in reason_codes:
        required_min = _compute_required_downpayment_pct(product, profile)
        if dp is not None and required_min is not None:
            lines.append(
                f"- ваш планируемый первоначальный взнос ({dp:.1f}% от стоимости квартиры) "
                f"ниже минимального по продукту ({required_min:.1f}%)."
            )
        else:
            lines.append("- ваш первоначальный взнос ниже минимального по этому продукту.")

    return "\n".join(lines)


# =========================
#  Основная функция подбора
# =========================

def match_mortgages(
    products: List[Dict[str, Any]],
    profile: Dict[str, Any],
    top_k: int = 2,
) -> Dict[str, Any]:
    """
    Подбор ипотечных кредитов (без расчёта платежей).

    Важно:
      profile["mortgage_program"] = "standard" / "bi_group" / "nrg_2_4" / "daho" / ...

    Возвращает структуру, удобную для JSON:
      {
        "profile": {...},
        "best_offers": [...],   # максимум top_k продуктов
        "near_miss": [...]      # максимум top_k продуктов, НО ТОЛЬКО если best_offers пуст
      }
    """
    amount_src = profile.get("requested_amount")
    if amount_src is None:
        raise ValueError("requested_amount обязателен для подбора ипотеки")

    amount = int(amount_src)
    term = profile.get("requested_term_months")  # может быть None

    best_offers: List[Dict[str, Any]] = []
    near_miss: List[Dict[str, Any]] = []

    # дефолт для всех ипотечных продуктов: только через филиалы
    default_channel = "branch"
    default_channel_text = (
        "Оформление этого ипотечного кредита возможно только через филиалы банка (ЦБУ), "
        "не через мобильное приложение."
    )

    for p in products:
        if p.get("category") != "mortgage":
            continue

        # 1) фильтр по программе (standard / bi_group / nrg_2_4 / daho / ...)
        if not _mortgage_program_ok(p, profile):
            continue

        # 2) цель
        if not _purpose_ok(p, profile):
            # если цель не совпала, near_miss не собираем
            continue

        # 3) собираем причины, почему клиент может не подходить под продукт
        miss_reasons = _collect_miss_reasons_mortgage(p, profile, amount, term)

        # если есть причины, а цель совпала → near miss (заполняем только если потом не найдём ни одного best_offers)
        if miss_reasons:
            if profile.get("purpose_keys"):
                near_miss.append(
                    {
                        "product": p,
                        "reason_codes": miss_reasons,
                        "reason_text": _build_near_miss_reason_text_mortgage(
                            p, profile, miss_reasons
                        ),
                        "channel": p.get("channel") or default_channel,
                        "channel_text": p.get("channel_text") or default_channel_text,
                    }
                )
            continue

        # Если сюда дошли — клиент укладывается в жёсткие условия продукта.
        # Проверяем eligibility и downpayment более "жёстко"
        if not _eligibility_ok_mortgage(p, profile):
            # не подходит, но причины уже есть в miss_reasons, тут можно не дублировать
            continue

        # downpayment для DAHO не режем, для остальных — да
        if p.get("mortgage_program") != "daho":
            dp = profile.get("downpayment_pct")
            if dp is not None:
                required_min = _compute_required_downpayment_pct(p, profile)
                if required_min is not None and float(dp) < float(required_min):
                    # формально это тоже miss, но так как есть eligibility-фильтр,
                    # сюда лучше не добавлять, чтобы не дублировать логику
                    continue

        apr = get_mortgage_apr(p, profile)
        if apr is None:
            # технически продукт не может быть отсортирован, пропустим
            continue

        score = 100.0 - float(apr)  # чем ниже ставка, тем выше score

        item: Dict[str, Any] = {
            "product": p,
            "apr": apr,
            "score": score,
            "channel": p.get("channel") or default_channel,
            "channel_text": p.get("channel_text") or default_channel_text,
        }

        # BI / NRG (две линии A/B)
        if p.get("bi_group_config"):
            bi_details = describe_bi_group_structure(p, profile)
            if bi_details is not None:
                item["bi_group_details"] = bi_details

        # DAHO
        if p.get("daho_config"):
            daho_details = describe_daho_structure(p, profile)
            if daho_details is not None:
                item["daho_details"] = daho_details

        best_offers.append(item)

    # сортируем подходящие продукты по score и берём максимум top_k
    best_offers.sort(key=lambda x: x["score"], reverse=True)
    best_offers = best_offers[:top_k]

    # Если нашли хотя бы один продукт → near_miss ОЧИЩАЕМ
    if best_offers:
        near_miss = []
    else:
        # Если ничего не нашли → показываем near-miss (максимум top_k)
        if len(near_miss) > top_k:
            near_miss = near_miss[:top_k]

    return {
        "profile": profile,
        "best_offers": best_offers,
        "near_miss": near_miss,
    }


# =========================
#  Сохранение в JSON
# =========================

def save_mortgage_response_to_json(
    response: Dict[str, Any],
    path: str,
) -> None:
    """
    Сохраняет результат подбора ипотеки в JSON-файл.

    Внутри response ожидаются ключи:
      - profile
      - best_offers
      - near_miss
    """
    with open(path, "w", encoding="utf-8") as f:
        json.dump(response, f, ensure_ascii=False, indent=2)


# =========================
#  Пример ручного запуска
# =========================

if __name__ == "__main__":
    products = load_products_from_yaml("mortgage_products.yml")

    # Пример профиля: стандартная ипотека
    profile_example = {
        "citizen_uz": True,
        "age": 30,
        "gender": "male",
        "income_proof": True,

        "requested_amount": 500_000_000,
        "requested_term_months": 180,    # 15 лет

        "purpose_segment": "housing",
        "purpose_keys": ["housing_repair"],
        "region_code": "tashkent",

        "mortgage_program": "standard",
        # downpayment_pct можно не указывать
    }

    result = match_mortgages(products, profile_example, top_k=2)
    save_mortgage_response_to_json(result, "mortgage_response.json")
    # from pprint import pprint
    # pprint(result)
