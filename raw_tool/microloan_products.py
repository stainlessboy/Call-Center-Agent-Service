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
          category: "microloan"
          ...
    """
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return data["products"]


# =========================
#  Хелперы по профилю клиента
# =========================

def _has_official_income(profile: Dict[str, Any]) -> bool:
    """
    Официальный доход считаем есть, если:
      - income_proof = True (есть справка/подтверждение дохода), ИЛИ
      - payroll_participant = True (участник проекта «Заработная плата»), ИЛИ
      - self_employed = True (самозанятый с доходом).
    """
    return bool(
        profile.get("income_proof")
        or profile.get("payroll_participant")
        or profile.get("self_employed")
    )


def _age_ok_for_product(product: Dict[str, Any], profile: Dict[str, Any]) -> bool:
    """
    Проверяет возраст клиента для конкретного продукта:
      - age_min / age_max (общие),
      - age_max_male / age_max_female (по полу).

    В profile желательно иметь:
      age: int
      gender: "male" / "female" (может быть None — тогда берём самый жёсткий максимум).
    """
    elig = product.get("eligibility") or {}
    age = profile.get("age")

    # Если возраст ещё не спросили у клиента — не режем на этом шаге.
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

    # Если gender не указан, а max_male/max_female есть — берём самый строгий максимум.
    if gender is None and (max_male is not None or max_female is not None):
        max_vals = [v for v in (max_male, max_female) if v is not None]
        if max_vals and age > min(max_vals):
            return False

    return True


def _any_product_matches_age(products: List[Dict[str, Any]], profile: Dict[str, Any]) -> bool:
    """
    Есть ли хотя бы один микрозайм, под который клиент подходит по возрасту.
    Используется в глобальных preconditions (ранний отказ).
    """
    for p in products:
        if p.get("category") != "microloan":
            continue
        if _age_ok_for_product(p, profile):
            return True
    return False


def check_microloan_preconditions(
    products: List[Dict[str, Any]],
    profile: Dict[str, Any],
) -> Optional[str]:
    """
    Глобальные "жёсткие" проверки для микрозаймов.

    Возвращает:
      - None                 → всё ок, можно продолжать матчинг;
      - "no_official_income" → нет официального дохода → сразу отказ;
      - "age_not_eligible"   → возраст не подходит ни под один продукт → отказ.
    """

    # Связка: участник зарплатного проекта ⇒ официальный доход есть
    if profile.get("payroll_participant") and not profile.get("income_proof"):
        profile["income_proof"] = True

    # 1) Официальный доход обязателен
    if not _has_official_income(profile):
        return "no_official_income"

    # 2) Возраст ни под один продукт не подходит
    if profile.get("age") is not None:
        if not _any_product_matches_age(products, profile):
            return "age_not_eligible"

    return None


# =========================
#  Интерпретатор условий when (apr/grace rules)
# =========================

_cond_pattern = re.compile(r"^(amount|term)\s*(<=|>=|==|<|>)\s*([0-9]+)$")


def _eval_when(condition: Optional[str],
               profile: Dict[str, Any],
               amount: int,
               term: int) -> bool:
    """
    Интерпретатор строки condition из apr_rules / grace_rules.

    Поддерживает:
      - логическое И: "a && b && c"
      - флаги: "income_proof", "payroll", "payroll_participant"
      - сравнения: "amount<=3000000", "term>24", "term==13"
    """
    if not condition:
        return True

    has_official_income = _has_official_income(profile)
    parts = [p.strip() for p in condition.split("&&") if p.strip()]

    for part in parts:
        # флаги
        if part == "income_proof":
            if not has_official_income:
                return False
            continue

        if part in ("payroll", "payroll_participant"):
            if not profile.get("payroll_participant", False):
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


# =========================
#  APR и льготный период
# =========================

def get_product_apr(
    product: Dict[str, Any],
    profile: Dict[str, Any],
    term_override: Optional[int] = None,
    amount_override: Optional[int] = None,
) -> Optional[float]:
    """
    Находит подходящее правило из product['apr_rules'].
    Если подходит несколько — берём минимальную ставку (лучшее для клиента).

    Можно передать:
      - term_override — считать APR для конкретного срока (без изменения profile),
      - amount_override — то же для суммы.
    """
    amount_src = amount_override if amount_override is not None else profile.get("requested_amount")
    term_src = term_override if term_override is not None else profile.get("requested_term_months")

    if amount_src is None or term_src is None:
        raise ValueError("Requested amount/term is required either in profile or as override")

    amount = int(amount_src)
    term = int(term_src)

    rules = product.get("apr_rules") or []
    applicable: List[float] = []

    for rule in rules:
        cond = rule.get("when")
        apr = rule["apr"]
        if _eval_when(cond, profile, amount, term):
            applicable.append(apr)

    if not applicable:
        return None
    return min(applicable)


def get_product_grace(
    product: Dict[str, Any],
    profile: Dict[str, Any],
    term_override: Optional[int] = None,
    amount_override: Optional[int] = None,
) -> Optional[int]:
    """
    Подбирает максимальный льготный период для данного срока по grace_rules.
    Если специальных правил нет — возвращает grace_default_months.

    term_override / amount_override работают так же, как в get_product_apr.
    """
    amount_src = amount_override if amount_override is not None else profile.get("requested_amount")
    term_src = term_override if term_override is not None else profile.get("requested_term_months")

    if amount_src is None or term_src is None:
        raise ValueError("Requested amount/term is required either in profile or as override")

    amount = int(amount_src)
    term = int(term_src)

    rules = product.get("grace_rules") or []
    best: Optional[int] = None

    for rule in rules:
        cond = rule.get("when")
        g = rule.get("grace_max_months")
        if g is None:
            continue
        if _eval_when(cond, profile, amount, term):
            best = max(best, g) if best is not None else g

    if best is not None:
        return best

    return product.get("grace_default_months")


# =========================
#  Базовые фильтры: сумма, срок, eligibility
# =========================

def _amount_ok(product: Dict[str, Any], amount: int) -> bool:
    amin = product.get("amount_min")
    amax = product.get("amount_max")
    if amin is not None and amount < amin:
        return False
    if amax is not None and amount > amax:
        return False
    return True


def _term_ok(product: Dict[str, Any], term: int) -> bool:
    tmin = product.get("term_min_months")
    tmax = product.get("term_max_months")
    if tmin is not None and term < tmin:
        return False
    if tmax is not None and term > tmax:
        return False
    return True


def _eligibility_ok(product: Dict[str, Any], profile: Dict[str, Any]) -> bool:
    """
    Проверяет общие eligibility-флаги + возраст (через _age_ok_for_product).
    Остальные "мягкие" вещи (оборот по карте и т.п.) можно добавлять позже.
    """
    elig = product.get("eligibility") or {}

    if elig.get("citizen_uz_required") and not profile.get("citizen_uz", False):
        return False

    if elig.get("self_employed_required") and not profile.get("self_employed", False):
        return False

    # возраст (min/max + male/female)
    if not _age_ok_for_product(product, profile):
        return False

    # TODO: сюда можно добавить income_history_months_required и прочие бизнес-правила

    return True


# =========================
#  Фильтр по цели (purpose)
# =========================

def _purpose_segment_ok(product: Dict[str, Any], profile: Dict[str, Any]) -> bool:
    """
    purpose_segment: "consumer" / "business" / "any"
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
    purposes: список ключей продукта, например ['personal_any', 'business_start']
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
      - совпал сегмент (consumer/business/any),
      - и (если client указал конкретные purpose_keys) есть пересечение ключей.
    """
    return _purpose_segment_ok(product, profile) and _purpose_keys_ok(product, profile)


# =========================
#  Подбор срока для продукта (без платежей)
# =========================

def _collect_candidate_terms(product: Dict[str, Any]) -> List[int]:
    """
    Возвращает список "интересных" сроков для продукта:
      - term_min_months
      - term_max_months
      - все значения term из условий apr_rules (term<=X, term>Y, term==Z)
    """
    tmin = product.get("term_min_months")
    tmax = product.get("term_max_months")
    if tmin is None or tmax is None:
        return []

    candidates = set()
    candidates.add(int(tmin))
    candidates.add(int(tmax))

    rules = product.get("apr_rules") or []
    for rule in rules:
        cond = rule.get("when")
        if not cond:
            continue
        parts = [p.strip() for p in cond.split("&&") if p.strip()]
        for part in parts:
            m = _cond_pattern.match(part)
            if not m:
                continue
            field, op, value_str = m.groups()
            if field != "term":
                continue
            value = int(value_str)
            if tmin <= value <= tmax:
                candidates.add(value)

    terms = sorted(candidates)
    return terms


def suggest_terms_for_product(
    product: Dict[str, Any],
    profile: Dict[str, Any],
    max_options: int = 3,
) -> List[Dict[str, Any]]:
    """
    Подбирает до max_options вариантов срока для данного продукта.
    Сейчас ИСКЛЮЧИТЕЛЬНО для выбора:
      - term_months  (срок)
      - apr          (ставка)
      - grace_months (льготный период)
      - score        (внутренний скоринговый балл для сортировки)

    Никаких платежей и лимитов по платежу пока не считаем.
    """
    amount_src = profile.get("requested_amount")
    if amount_src is None:
        raise ValueError("requested_amount is required in profile for term suggestions")

    amount = int(amount_src)
    candidate_terms = _collect_candidate_terms(product)
    if not candidate_terms:
        return []

    offers: List[Dict[str, Any]] = []

    for term in candidate_terms:
        # APR и льготный период считаем с override по сроку
        apr = get_product_apr(product, profile, term_override=term, amount_override=amount)
        if apr is None:
            continue

        grace = get_product_grace(product, profile, term_override=term, amount_override=amount)

        score = 100.0 - float(apr)
        if grace:
            score += grace * 0.2

        offers.append(
            {
                "term_months": term,
                "apr": apr,
                "grace_months": grace,
                "score": score,
            }
        )

    if not offers:
        return []

    # сортируем по score
    offers.sort(key=lambda x: x["score"], reverse=True)

    # выберем не более max_options с разными сроками
    result: List[Dict[str, Any]] = []
    seen_terms = set()
    for off in offers:
        t = off["term_months"]
        if t in seen_terms:
            continue
        result.append(off)
        seen_terms.add(t)
        if len(result) >= max_options:
            break

    return result


# =========================
#  Старые матчеры (для совместимости, максимум 2 продукта)
# =========================

def match_microloans(
    products: List[Dict[str, Any]],
    profile: Dict[str, Any],
    top_k: int = 2,
) -> List[Dict[str, Any]]:
    """
    Классический подбор: клиент уже указал срок (requested_term_months),
    возвращаем максимум два продукта.
    """
    top_limit = min(top_k, 2)

    amount = int(profile["requested_amount"])
    term = int(profile["requested_term_months"])

    candidates: List[Dict[str, Any]] = []

    for p in products:
        if p.get("category") != "microloan":
            continue

        if not _purpose_ok(p, profile):
            continue
        if not _amount_ok(p, amount):
            continue
        if not _term_ok(p, term):
            continue
        if not _eligibility_ok(p, profile):
            continue

        apr = get_product_apr(p, profile)
        if apr is None:
            continue

        grace = get_product_grace(p, profile)

        base_score = 100.0 - float(apr)
        if grace:
            base_score += grace * 0.2

        candidates.append(
            {
                "product": p,
                "apr": apr,
                "grace_months": grace,
                "score": base_score,
            }
        )

    candidates.sort(key=lambda x: x["score"], reverse=True)
    return candidates[:top_limit]


def match_microloans_auto_term(
    products: List[Dict[str, Any]],
    profile: Dict[str, Any],
    top_k_products: int = 2,
    term_options_per_product: int = 3,
) -> List[Dict[str, Any]]:
    """
    Авто-подбор микрозаймов без заданного срока.
    Возвращаем максимум два продукта.
    """
    top_limit = min(top_k_products, 2)

    amount_src = profile.get("requested_amount")
    if amount_src is None:
        raise ValueError("requested_amount is required in profile for auto-term matching")

    candidates: List[Dict[str, Any]] = []

    for p in products:
        if p.get("category") != "microloan":
            continue

        # цель, сумма, возраст/гражданство/доход — как раньше
        if not _purpose_ok(p, profile):
            continue
        if not _amount_ok(p, int(amount_src)):
            continue
        if not _eligibility_ok(p, profile):
            continue

        # подбираем варианты сроков для этого продукта
        offers = suggest_terms_for_product(
            product=p,
            profile=profile,
            max_options=term_options_per_product,
        )
        if not offers:
            continue

        best_score = max(off["score"] for off in offers)
        candidates.append(
            {
                "product": p,
                "offers": offers,
                "best_score": best_score,
            }
        )

    candidates.sort(key=lambda x: x["best_score"], reverse=True)
    return candidates[:top_limit]


# =========================
#  "Ипотечный" формат ответа + near_miss
# =========================

_PRECONDITION_REASON_TEXTS: Dict[str, str] = {
    "no_official_income": (
        "Для оформления микрозайма необходим подтверждённый официальный доход "
        "(справка о доходах, участие в зарплатном проекте или статус самозанятого)."
    ),
    "age_not_eligible": (
        "На данный момент ваш возраст не соответствует возрастным ограничениям по микрозаймам банка."
    ),
}


def _build_flag_reason(
    ok: bool,
    ok_text: str,
    fail_text: str,
    code_ok: str,
    code_fail: str,
) -> Dict[str, Any]:
    if ok:
        return {"ok": True, "code": code_ok, "text": ok_text}
    return {"ok": False, "code": code_fail, "text": fail_text}


def _first_failed_reason(checks: Dict[str, Dict[str, Any]], order: List[str]) -> Optional[str]:
    for key in order:
        check = checks.get(key)
        if check and not check.get("ok", False):
            return str(check.get("text"))
    return None


def _explain_product_for_known_term(
    product: Dict[str, Any],
    profile: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Детальная диагностика продукта для режима, когда клиент указал срок.
    """
    amount = int(profile["requested_amount"])
    term = int(profile["requested_term_months"])

    purpose_ok = _purpose_ok(product, profile)
    amount_ok = _amount_ok(product, amount)
    term_ok = _term_ok(product, term)
    eligibility_ok = _eligibility_ok(product, profile)

    apr: Optional[float] = None
    grace: Optional[int] = None
    score: Optional[float] = None

    if purpose_ok and amount_ok and term_ok and eligibility_ok:
        apr = get_product_apr(product, profile)
        if apr is not None:
            grace = get_product_grace(product, profile)
            score = 100.0 - float(apr)
            if grace:
                score += grace * 0.2

    apr_ok = apr is not None

    checks: Dict[str, Dict[str, Any]] = {
        "purpose": _build_flag_reason(
            purpose_ok,
            ok_text="Продукт соответствует выбранной цели кредита.",
            fail_text="Продукт не соответствует выбранной цели кредита.",
            code_ok="purpose_match",
            code_fail="purpose_mismatch",
        ),
        "amount": _build_flag_reason(
            amount_ok,
            ok_text="Запрошенная сумма укладывается в лимиты по продукту.",
            fail_text="Запрошенная сумма не укладывается в минимальную/максимальную сумму по продукту.",
            code_ok="amount_ok",
            code_fail="amount_out_of_range",
        ),
        "term": _build_flag_reason(
            term_ok,
            ok_text="Запрошенный срок укладывается в лимиты по продукту.",
            fail_text="Запрошенный срок не укладывается в минимальный/максимальный срок по продукту.",
            code_ok="term_ok",
            code_fail="term_out_of_range",
        ),
        "eligibility": _build_flag_reason(
            eligibility_ok,
            ok_text="Вы подходите под базовые требования по гражданству, возрасту и статусу занятости.",
            fail_text="Вы не соответствуете базовым требованиям по гражданству, возрасту или статусу занятости.",
            code_ok="eligibility_ok",
            code_fail="eligibility_fail",
        ),
        "apr_rule": _build_flag_reason(
            apr_ok,
            ok_text="Для указанной суммы и срока найдена ставка по тарифу.",
            fail_text="Для указанной суммы и срока не удалось подобрать ставку по тарифу.",
            code_ok="apr_found",
            code_fail="apr_not_found",
        ),
    }

    eligible = purpose_ok and amount_ok and term_ok and eligibility_ok and apr_ok

    offer: Optional[Dict[str, Any]] = None
    if eligible:
        offer = {
            "term_months": term,
            "apr": apr,
            "grace_months": grace,
            "score": score,
        }

    main_fail_reason: Optional[str] = None
    if not eligible:
        main_fail_reason = _first_failed_reason(
            checks,
            order=["purpose", "amount", "term", "eligibility", "apr_rule"],
        )

    return {
        "product_id": product.get("id"),
        "product_name": product.get("name"),
        "eligible": eligible,
        "main_fail_reason": main_fail_reason,
        "checks": checks,
        "offer": offer,
        "product_data": product,
    }


def _explain_product_auto_term(
    product: Dict[str, Any],
    profile: Dict[str, Any],
    term_options_per_product: int = 3,
) -> Dict[str, Any]:
    """
    Детальная диагностика продукта для режима, когда срок подбираем автоматически.
    """
    amount = int(profile["requested_amount"])

    purpose_ok = _purpose_ok(product, profile)
    amount_ok = _amount_ok(product, amount)
    eligibility_ok = _eligibility_ok(product, profile)

    checks: Dict[str, Dict[str, Any]] = {
        "purpose": _build_flag_reason(
            purpose_ok,
            ok_text="Продукт соответствует выбранной цели кредита.",
            fail_text="Продукт не соответствует выбранной цели кредита.",
            code_ok="purpose_match",
            code_fail="purpose_mismatch",
        ),
        "amount": _build_flag_reason(
            amount_ok,
            ok_text="Запрошенная сумма укладывается в лимиты по продукту.",
            fail_text="Запрошенная сумма не укладывается в минимальную/максимальную сумму по продукту.",
            code_ok="amount_ok",
            code_fail="amount_out_of_range",
        ),
        "eligibility": _build_flag_reason(
            eligibility_ok,
            ok_text="Вы подходите под базовые требования по гражданству, возрасту и статусу занятости.",
            fail_text="Вы не соответствуете базовым требованиям по гражданству, возрасту или статусу занятости.",
            code_ok="eligibility_ok",
            code_fail="eligibility_fail",
        ),
    }

    eligible_base = purpose_ok and amount_ok and eligibility_ok

    offers: List[Dict[str, Any]] = []
    best_score: Optional[float] = None

    if eligible_base:
        offers = suggest_terms_for_product(
            product=product,
            profile=profile,
            max_options=term_options_per_product,
        )
        if offers:
            best_score = max(off["score"] for off in offers)

    eligible_for_offers = bool(offers)

    main_fail_reason: Optional[str] = None
    if not eligible_base:
        main_fail_reason = _first_failed_reason(
            checks,
            order=["purpose", "amount", "eligibility"],
        )
    elif not eligible_for_offers:
        main_fail_reason = (
            "По базовым условиям вы подходите под продукт, но для запрошенной суммы "
            "не удалось подобрать подходящий срок по действующим ставкам."
        )

    return {
        "product_id": product.get("id"),
        "product_name": product.get("name"),
        "eligible_base": eligible_base,
        "eligible_for_offers": eligible_for_offers,
        "main_fail_reason": main_fail_reason,
        "checks": checks,
        "offers": offers,
        "best_offer_score": best_score,
        "product_data": product,
    }


def build_microloan_response_known_term(
    products: List[Dict[str, Any]],
    profile: Dict[str, Any],
    top_k: int = 2,
) -> Dict[str, Any]:
    """
    Формат, похожий на ипотеку. Клиент указал срок.
    Если есть подходящие продукты — near_miss пустой.
    Максимум два продукта.
    """
    top_limit = min(top_k, 2)

    precond = check_microloan_preconditions(products, profile)
    if precond is not None:
        return {
            "status": "rejected",
            "reason_code": precond,
            "reason_text": _PRECONDITION_REASON_TEXTS.get(
                precond,
                "Вы не подходите под базовые требования для оформления микрозайма.",
            ),
            "products": [],
            "best_offers": [],
            "near_miss": [],
        }

    explained: List[Dict[str, Any]] = []
    for p in products:
        if p.get("category") != "microloan":
            continue
        explained.append(_explain_product_for_known_term(p, profile))

    eligible_with_offer = [
        item
        for item in explained
        if item["eligible"] and item.get("offer") is not None
    ]

    eligible_with_offer.sort(
        key=lambda x: x["offer"]["score"] if x["offer"] and x["offer"]["score"] is not None else -1,
        reverse=True,
    )

    best_offers = eligible_with_offer[:top_limit]

    near_miss: List[Dict[str, Any]] = []
    # В микрозаймах, если продукт найден → near_miss не показываем
    if not best_offers:
        near_miss_candidates: List[Dict[str, Any]] = []
        for item in explained:
            if item["eligible"]:
                continue
            checks = item["checks"]
            purpose_ok = checks["purpose"]["ok"]
            eligibility_ok = checks["eligibility"]["ok"]
            if not purpose_ok or not eligibility_ok:
                continue

            amount_ok = checks["amount"]["ok"]
            term_ok = checks["term"]["ok"]
            apr_ok = checks["apr_rule"]["ok"]

            # Клиент в целом подходит, но не дотянул по сумме/сроку/ставке
            if (not amount_ok) or (not term_ok) or (not apr_ok):
                near_miss_candidates.append(item)

        near_miss = near_miss_candidates[:2]

    return {
        "status": "ok",
        "reason_code": None,
        "reason_text": None,
        "requested_amount": int(profile["requested_amount"]),
        "requested_term_months": int(profile["requested_term_months"]),
        "best_offers": best_offers,
        "near_miss": near_miss,
        "products": explained,
    }


def build_microloan_response_auto_term(
    products: List[Dict[str, Any]],
    profile: Dict[str, Any],
    top_k_products: int = 2,
    term_options_per_product: int = 3,
) -> Dict[str, Any]:
    """
    Формат, похожий на ипотеку, для авто-подбора срока.
    Максимум два продукта. Если есть best_offers — near_miss пустой.
    """
    top_limit = min(top_k_products, 2)

    precond = check_microloan_preconditions(products, profile)
    if precond is not None:
        return {
            "status": "rejected",
            "reason_code": precond,
            "reason_text": _PRECONDITION_REASON_TEXTS.get(
                precond,
                "Вы не подходите под базовые требования для оформления микрозайма.",
            ),
            "products": [],
            "best_offers": [],
            "near_miss": [],
        }

    explained: List[Dict[str, Any]] = []
    for p in products:
        if p.get("category") != "microloan":
            continue
        explained.append(
            _explain_product_auto_term(
                product=p,
                profile=profile,
                term_options_per_product=term_options_per_product,
            )
        )

    eligible_items = [
        item
        for item in explained
        if item["eligible_for_offers"] and item.get("best_offer_score") is not None
    ]

    eligible_items.sort(
        key=lambda x: x["best_offer_score"] if x["best_offer_score"] is not None else -1,
        reverse=True,
    )

    best_offers = eligible_items[:top_limit]

    near_miss: List[Dict[str, Any]] = []
    # Если есть best_offers → near_miss не показываем
    if not best_offers:
        near_miss_candidates: List[Dict[str, Any]] = []
        for item in explained:
            checks = item["checks"]
            purpose_ok = checks["purpose"]["ok"]
            eligibility_ok = checks["eligibility"]["ok"]
            amount_ok = checks["amount"]["ok"]

            # Вариант 1: всё ок по назначению и требованиям, но сумма вне диапазона
            if purpose_ok and eligibility_ok and not amount_ok:
                near_miss_candidates.append(item)
                continue

            # Вариант 2: базово подходит, но не смогли подобрать срок/ставку
            if item["eligible_base"] and not item["eligible_for_offers"]:
                near_miss_candidates.append(item)

        near_miss = near_miss_candidates[:2]

    return {
        "status": "ok",
        "reason_code": None,
        "reason_text": None,
        "requested_amount": int(profile["requested_amount"]),
        "best_offers": best_offers,
        "near_miss": near_miss,
        "products": explained,
    }


# =========================
#  Пример ручного запуска + запись в JSON
# =========================

if __name__ == "__main__":
    try:
        products = load_products_from_yaml("microloan_products.yml")
    except FileNotFoundError:
        print("microloan_products.yml not found; nothing to test.")
        products = []

    if products:
        profile_example_auto = {
            "citizen_uz": True,
            "age": 56,
            "gender": "male",
            "self_employed": False,
            "income_proof": True,
            "payroll_participant": False,
            "requested_amount": 120_000_000,
            "purpose_segment": "consumer",
            "purpose_keys": ["personal_any"],
        }

        # -------- Авто-подбор срока --------
        resp_auto = build_microloan_response_auto_term(
            products,
            profile_example_auto,
            top_k_products=2,
            term_options_per_product=3,
        )

        with open("microloan_response_auto_term.json", "w", encoding="utf-8") as f:
            json.dump(resp_auto, f, ensure_ascii=False, indent=2)

        # -------- Подбор с заданным сроком --------
        profile_example_known = dict(profile_example_auto, requested_term_months=6)

        resp_known = build_microloan_response_known_term(
            products,
            profile_example_known,
            top_k=2,
        )

        with open("microloan_response_known_term.json", "w", encoding="utf-8") as f:
            json.dump(resp_known, f, ensure_ascii=False, indent=2)

        print("JSON-ответы сохранены в microloan_response_auto_term.json и microloan_response_known_term.json")
    else:
        print("microloan_products.yml not found; nothing to test.")
