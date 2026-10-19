#!/usr/bin/env python3
"""scripts/diag_model.py — оценочный прогон LLM по узбекским и русским вопросам.

Гоняет набор вопросов на каждом языке через РЕАЛЬНЫЙ слой агента:
реальные 11 инструментов (`_FAQ_TOOLS`) + системная политика
(`get_system_policy`) + отключённый thinking — то есть ровно так, как
node_faq обращается к модели. По каждому вопросу печатает:

    • что сделала модель — вызвала инструмент (имя + аргументы) или ответила текстом
    • совпало ли это с ожиданием (для запросов продукта/филиала/валюты ждём инструмент)
    • токены (prompt / completion / total)
    • время ответа

В конце — сводка по каждому языку: доля сработавших инструментов там, где их
ждали, среднее время и суммарные токены. Это и есть критерий пригодности модели.

По умолчанию бьёт в новую Qwen3-Next-80B (vllm-model-access (2).md).
Любой OpenAI-совместимый endpoint задаётся флагами.

Использование:
    python3 scripts/diag_model.py                      # 80B, temp 0.3
    python3 scripts/diag_model.py --temp 0.1           # ниже температура (надёжнее tool-routing)
    python3 scripts/diag_model.py --runs 2             # по 2 прогона на вопрос (вызов стохастичен)
    python3 scripts/diag_model.py --model X --url http://host:port/v1 --token T
    python3 scripts/diag_model.py --coder              # пресет для Qwen3-Coder-30B

Токены пресетных эндпоинтов НЕ хранятся в коде (секреты в git не коммитим).
Передай токен одним из способов (по приоритету):
    --token T                  # явный CLI-флаг
    DIAG_TOKEN_<PRESET>=...    # напр. DIAG_TOKEN_NEXT80B, DIAG_TOKEN_CODER, DIAG_TOKEN_GEMMA
    DIAG_MODEL_TOKEN=...       # общий фолбэк-токен для любого пресета
Без токена ни в одном из этих мест скрипт завершится понятной ошибкой.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

# ── Пресеты моделей из vllm-*-access.md ───────────────────────────────────────
# Токены сюда НЕ вписывать — см. docstring модуля (--token / DIAG_TOKEN_<PRESET> /
# DIAG_MODEL_TOKEN). Раньше тут лежали реальные bearer-токены к внешним
# GPU-инстансам — вынесены в env, чтобы не утекать в git-историю.
PRESETS = {
    "next80b": {
        "model": "cyankiwi/Qwen3-Next-80B-A3B-Instruct-AWQ-4bit",
        "url": "http://154.42.3.37:21133/v1",
    },
    "coder": {
        "model": "cyankiwi/Qwen3-Coder-30B-A3B-Instruct-AWQ-4bit",
        "url": "http://154.42.3.36:32448/v1",
    },
    "gemma": {
        "model": "cyankiwi/gemma-4-31B-it-AWQ-4bit",
        "url": "http://154.42.3.11:29510/v1",
    },
    # ── Внутренний on-prem эндпоинт AsakaBank (NVIDIA B200), FP8, контекст 32k ──
    # ⚠️ Тестовый режим: реальные персональные/банковские данные слать НЕЛЬЗЯ.
    "asaka26b": {
        "model": "gemma-4-26b",  # RedHatAI/gemma-4-26B-A4B-it-FP8-dynamic (MoE, 4B активных)
        "url": "https://runai.gpu.uz/asaka-b200-test-23-07-2026-56/gemma-serve/v1",
    },
    "asaka31b": {
        "model": "gemma-4-31b",  # RedHatAI/gemma-4-31B-it-FP8-dynamic (dense)
        "url": "https://runai.gpu.uz/asaka-b200-test-23-07-2026-56/gemma-serve/url-1/v1",
    },
    # ── Тестовый vast.ai с картой как в банке: RTX 4080 16 GB, llama-server ──
    # Доступен только через SSH-туннель на localhost:18000 (gemma-test-server.md),
    # API-ключ не проверяется. ~5 tok/s — отсюда большой таймаут.
    # ⚠️ Сторонний сервер: только синтетические данные.
    "vast31b": {
        "model": "gemma-4-31b-it-qat",  # Gemma 4 31B dense, QAT Q4_0, контекст 16k
        "url": "http://localhost:18000/v1",
        "token": "not-needed",
        "timeout": 300.0,
    },
    # Второй vast.ai, та же RTX 4080 16 GB — туннель на localhost:18001
    # (gemma-26b-test-server.md). MoE, целиком в видеопамяти, ~130 tok/s.
    "vast26b": {
        "model": "gemma-4-26b-a4b-it-qat",  # Gemma 4 26B-A4B MoE, QAT Q4_0, контекст 16k
        "url": "http://localhost:18001/v1",
        "token": "not-needed",
        "timeout": 120.0,
    },
}

# ── Вопросы: (текст, ожидание) ───────────────────────────────────────────────
# ожидание: "tool" — должен сработать инструмент (запрос продукта/филиала/валюты)
#           "none" — инструмент НЕ нужен (приветствие)
#           "any"  — допустимо и то и другое (общий FAQ / операторские операции)
# второй элемент кортежа — подсказка по «правильному» инструменту (для глаза).
QUESTIONS = {
    "ru": [
        ("Хочу оформить ипотеку",            "tool", "get_products/show_credit_menu"),
        ("Покажите ваши автокредиты",        "tool", "get_products(autoloan)"),
        ("Какие вклады у вас есть?",         "tool", "get_products(deposit)"),
        ("Хочу открыть карту",               "tool", "get_products(debit_card)"),
        ("Мне нужен кредит",                 "tool", "show_credit_menu"),
        ("Где ближайший филиал в Ташкенте?", "tool", "find_office"),
        ("Какой сегодня курс доллара?",      "tool", "get_currency_info"),
        ("Что такое эскроу-счёт?",           "any",  "faq_lookup/текст"),
        ("Как заблокировать карту?",         "any",  "request_operator/faq_lookup"),
        ("Здравствуйте!",                    "none", "—"),
    ],
    "uz": [
        ("Ipoteka olmoqchiman",                 "tool", "get_products/show_credit_menu"),
        ("Avtokreditlaringizni ko'rsating",     "tool", "get_products(autoloan)"),
        ("Qanday omonatlaringiz bor?",          "tool", "get_products(deposit)"),
        ("Karta ochmoqchiman",                  "tool", "get_products(debit_card)"),
        ("Menga kredit kerak",                  "tool", "show_credit_menu"),
        ("Toshkentda eng yaqin filial qayerda?","tool", "find_office"),
        ("Bugun dollar kursi qancha?",          "tool", "get_currency_info"),
        ("Eskrou hisob nima?",                  "any",  "faq_lookup/matn"),
        ("Kartani qanday bloklash mumkin?",     "any",  "request_operator/faq_lookup"),
        ("Assalomu alaykum!",                   "none", "—"),
    ],
}


# Как пишут настоящие клиенты: сленг, опечатки, кириллица, смесь языков.
# Отдельно от QUESTIONS, чтобы старые цифры оставались сопоставимыми:
# --set extra гоняет только этот набор. Ключи — язык системной политики.
QUESTIONS_EXTRA = {
    "uz": [
        # латиница, разговорная
        ("ipotekaga qanaqa shartlar bor",       "tool", "get_products(mortgage)/faq_lookup"),
        ("mashinaga kredit olsam boladimi",     "tool", "get_products(autoloan)"),
        ("omonat ochmoqchidim",                 "tool", "get_products(deposit)"),
        ("dollarga karta kerak",                "tool", "get_products(fx_card)"),
        ("kredit kere",                         "tool", "show_credit_menu"),
        # валюта не названа: по правилу get_currency_info модель уточняет
        ("kurs qancha bugun",                   "any",  "уточнение валюты/get_currency_info"),
        ("operator bilan gaplashmoqchiman",     "tool", "request_operator"),
        ("kartam bloklanib qoldi",              "any",  "faq_lookup/request_operator, НЕ каталог"),
        ("salom",                               "none", "—"),
        # кириллица
        ("ипотека олмокчиман",                  "tool", "get_products(mortgage)"),
        ("автокредит борми",                    "tool", "get_products(autoloan)"),
        ("омонатларингизни курсатинг",          "tool", "get_products(deposit)"),
        ("карта очмокчиман",                    "tool", "get_products(debit_card)"),
        ("менга кредит керак",                  "tool", "show_credit_menu"),
        ("таълим кредити бор ми",               "tool", "get_products(education_credit)"),
        ("долар курси канча",                   "tool", "get_currency_info"),
        ("Тошкентда филиал кайерда",            "tool", "find_office"),
        ("картани кандай блоклайман",           "any",  "faq_lookup/request_operator, НЕ каталог"),
        ("ассалому алайкум",                    "none", "—"),
    ],
    "ru": [
        ("ипотеку хочу",                        "tool", "get_products(mortgage)"),
        ("че по вкладам",                       "tool", "get_products(deposit)"),
        ("кредит на машину надо",               "tool", "get_products(autoloan)"),
        ("карту открыть хочу",                  "tool", "get_products(debit_card)"),
        ("микрозайм дайте",                     "tool", "get_products(microloan)"),
        ("как получить карту",                  "any",  "faq_lookup/get_products"),
        ("потерял карту что делать",            "any",  "faq_lookup/request_operator, НЕ каталог"),
    ],
}


# Контрольный набор: написан ДО правки описаний инструментов, и ни одна фраза
# отсюда (как и из QUESTIONS / QUESTIONS_EXTRA) не должна попадать в примеры
# описаний — иначе тест мерит подгонку, а не обобщение. --set holdout.
QUESTIONS_HOLDOUT = {
    "uz": [
        # латиница
        ("uy olish uchun kredit bormi",          "tool", "get_products(mortgage)"),
        ("avtokredit shartlari qanaqa",          "tool", "get_products(autoloan)/faq_lookup"),
        ("jamg'arma depozit ochsam bo'ladimi",   "tool", "get_products(deposit)"),
        ("kontrakt to'lash uchun kredit kerak",  "tool", "get_products(education_credit)"),
        ("mikroqarz olsam bo'ladimi",            "tool", "get_products(microloan)"),
        ("humo karta ochib bersangiz",           "tool", "get_products(debit_card)"),
        ("visa karta kerak edi",                 "tool", "get_products(fx_card)"),
        ("qarz olmoqchi edim",                   "tool", "show_credit_menu"),
        ("yevro kursi necha pul",                "tool", "get_currency_info"),
        ("Chilonzorda filiallaringiz bormi",     "tool", "find_office"),
        ("jonli odam bilan gaplashsam bo'ladimi","tool", "request_operator"),
        ("kartamni yo'qotib qo'ydim nima qilay", "any",  "faq_lookup/request_operator, НЕ каталог"),
        ("rahmat katta",                         "none", "—"),
        # кириллица
        ("уй жой учун кредит керак",             "tool", "get_products(mortgage)"),
        ("машина олишга кредит борми",           "tool", "get_products(autoloan)"),
        ("депозит фоизлари канча",               "tool", "get_products(deposit)/faq_lookup"),
        ("валюта карта очиш",                    "tool", "get_products(fx_card)"),
        ("евро курси",                           "tool", "get_currency_info"),
        ("Самаркандда филиал каерда",            "tool", "find_office"),
        ("оператор керак",                       "tool", "request_operator"),
        ("картам блок булиб колди",              "any",  "faq_lookup/request_operator, НЕ каталог"),
        ("салом яхшимисиз",                      "none", "—"),
    ],
    "ru": [
        ("нужна ипотека на вторичку",            "tool", "get_products(mortgage)"),
        ("есть автокредит без первоначального взноса?", "tool", "get_products(autoloan)/faq_lookup"),
        ("какие проценты по депозитам",          "tool", "get_products(deposit)/faq_lookup"),
        ("хочу валютную карту",                  "tool", "get_products(fx_card)"),
        ("дайте кредит",                         "tool", "show_credit_menu"),
        ("курс евро",                            "tool", "get_currency_info"),
        ("соедините с живым человеком",          "tool", "request_operator"),
        ("у меня украли карту",                  "any",  "faq_lookup/request_operator, НЕ каталог"),
        ("спасибо",                              "none", "—"),
    ],
}

QUESTION_SETS = {"base": QUESTIONS, "extra": QUESTIONS_EXTRA, "holdout": QUESTIONS_HOLDOUT}


def _verdict(expected: str, fired: bool) -> str:
    if expected == "tool":
        return "✅" if fired else "❌"
    if expected == "none":
        return "✅" if not fired else "⚠️"
    return "·"  # any


_ALT_RE = re.compile(r"^([a-z_]+)(?:\((\w+)\))?$")


def _strict_ok(expected: str, hint: str, tool_calls: list) -> bool:
    """Строгая проверка: та ли функция и та ли категория.

    `hint` — подсказка из набора вопросов: «get_products(deposit)/faq_lookup»
    перечисляет допустимые вызовы; всё, что не похоже на имя инструмента
    («текст», «matn», «уточнение валюты»), значит, что допустим и текстовый
    ответ. «НЕ каталог» после запятой запрещает get_products.
    """
    head, _, tail = hint.partition(",")
    allowed: list[tuple[str, str | None]] = []
    for alt in (a.strip() for a in head.split("/")):
        m = _ALT_RE.match(alt)
        if m:
            allowed.append((m.group(1), m.group(2)))
    forbid_catalog = "НЕ каталог" in tail
    if expected == "none":
        return not tool_calls
    if not tool_calls:
        return expected == "any"
    name = tool_calls[0]["name"]
    args = tool_calls[0].get("args") or {}
    if forbid_catalog and name == "get_products":
        return False
    if expected == "any":
        # «any» — модель вправе ответить по-разному (FAQ, оператор,
        # уточнение); ошибка только то, что запрещено явно.
        return True
    return any(name == n and (c is None or args.get("category") == c) for n, c in allowed)


async def main() -> int:
    parser = argparse.ArgumentParser(description="Оценочный прогон LLM (uz/ru)")
    parser.add_argument("--preset", choices=list(PRESETS), default="next80b",
                        help="пресет модели (по умолчанию next80b)")
    parser.add_argument("--coder", action="store_true", help="ярлык для --preset coder")
    parser.add_argument("--gpt", action="store_true",
                        help="тест OpenAI-модели (по умолчанию gpt-5.4-mini, ключ из OPENAI_API_KEY)")
    parser.add_argument("--model", default=None, help="имя модели (переопределяет пресет)")
    parser.add_argument("--url", default=None, help="base_url (.../v1)")
    parser.add_argument("--token", default=None, help="Bearer-токен / api_key")
    parser.add_argument("--temp", type=float, default=0.3, help="температура (агент использует 0.3)")
    parser.add_argument("--max-tokens", type=int, default=3000)
    parser.add_argument("--runs", type=int, default=1, help="прогонов на вопрос")
    parser.add_argument("--timeout", type=float, default=None,
                        help="таймаут запроса, с (по умолчанию из пресета, иначе 60)")
    parser.add_argument("--lang", choices=["ru", "uz", "both"], default="both")
    parser.add_argument("--dump", default=None,
                        help="JSONL-файл: сырой ответ модели на каждый прогон "
                             "(content, tool_calls, finish_reason) — для разбора сбоев")
    parser.add_argument("--set", dest="qset", choices=list(QUESTION_SETS), default="base",
                        help="набор вопросов: base (исходный), extra (сленг, кириллица), "
                             "holdout (контрольный, не используется в примерах описаний)")
    parser.add_argument("--salvage", action="store_true",
                        help="применить к текстовым ответам ту же страховку, что node_faq "
                             "(app/agent/tool_salvage.py): вызов, написанный текстом, или "
                             "явная просьба о продукте/операторе → вызов инструмента")
    args = parser.parse_args()

    load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=False)

    import os

    # Импортируем после sys.path/​dotenv.
    from langchain_openai import ChatOpenAI
    from langchain_core.messages import HumanMessage, SystemMessage
    from app.agent.llm import (
        extract_text_content, extract_token_usage,
        _is_reasoning_model, _default_reasoning_effort, _needs_responses_api,
    )
    from app.agent.tools import _FAQ_TOOLS
    from app.agent.i18n import get_system_policy
    try:
        from app.agent.tool_salvage import rescue_tool_call
    except ImportError:  # версия бота без страховки
        rescue_tool_call = None
    if args.salvage and rescue_tool_call is None:
        parser.error("--salvage: в этой версии нет app/agent/tool_salvage.py")

    tool_names = [t.name for t in _FAQ_TOOLS]

    if args.gpt:
        # OpenAI-путь: ключ из OPENAI_API_KEY, без base_url/enable_thinking.
        # gpt-5.x — reasoning-модель: нужен reasoning_effort + responses API
        # (ровно как в app/agent/llm.py для прод-агента).
        model = args.model or "gpt-5.4-mini"
        kwargs = dict(
            model=model, api_key=os.getenv("OPENAI_API_KEY"),
            temperature=args.temp, max_tokens=args.max_tokens,
            timeout=60.0, max_retries=1,
        )
        base_url = os.getenv("OPENAI_BASE_URL")
        if base_url:
            kwargs["base_url"] = base_url
        if _is_reasoning_model(model):
            kwargs["reasoning_effort"] = os.getenv("REASONING_EFFORT") or _default_reasoning_effort(model)
        if _needs_responses_api(model):
            kwargs["use_responses_api"] = True
        chat = ChatOpenAI(**kwargs)
        url = base_url or "api.openai.com (OpenAI)"
    else:
        preset_name = "coder" if args.coder else args.preset
        preset = PRESETS[preset_name]
        model = args.model or preset["model"]
        url = args.url or preset["url"]
        token = (
            args.token
            or os.getenv(f"DIAG_TOKEN_{preset_name.upper()}")
            or os.getenv("DIAG_MODEL_TOKEN")
            or preset.get("token")
        )
        if not token:
            parser.error(
                f"No token for preset '{preset_name}'. Pass --token, or set "
                f"DIAG_TOKEN_{preset_name.upper()} or DIAG_MODEL_TOKEN in the environment."
            )
        chat = ChatOpenAI(
            model=model, base_url=url, api_key=token,
            temperature=args.temp, max_tokens=args.max_tokens,
            timeout=args.timeout or preset.get("timeout", 60.0), max_retries=1,
            extra_body={"chat_template_kwargs": {"enable_thinking": False}},
        )

    bound = chat.bind_tools(_FAQ_TOOLS)

    print("=" * 78)
    print(f" МОДЕЛЬ : {model}")
    print(f" URL    : {url}")
    print(f" temp={args.temp}  max_tokens={args.max_tokens}  runs={args.runs}  tools={len(_FAQ_TOOLS)}")
    print("=" * 78)

    questions = QUESTION_SETS[args.qset]
    langs = ["ru", "uz"] if args.lang == "both" else [args.lang]
    grand = {}
    dump = open(args.dump, "w", encoding="utf-8") if args.dump else None

    for lang in langs:
        policy = get_system_policy(lang, "default")
        sys_msg = SystemMessage(content=policy)
        print(f"\n{'─'*78}\n  ЯЗЫК: {lang.upper()}\n{'─'*78}")

        tool_expected = tool_fired = tool_rescued = 0
        strict_total = strict_ok = 0
        t_sum = 0.0
        tok_prompt = tok_compl = 0

        for i, (q, expected, hint) in enumerate(questions[lang], 1):
            for run in range(args.runs):
                # Обрыв связи (SSH-туннель, сеть) не должен молча уменьшать
                # выборку: повторяем запрос, прежде чем сдаться.
                r = None
                for attempt in range(1, 4):
                    t0 = time.perf_counter()
                    try:
                        r = await bound.ainvoke([sys_msg, HumanMessage(content=q)])
                        dt = time.perf_counter() - t0
                        break
                    except Exception as exc:  # noqa: BLE001
                        print(f"[{lang} {i:2}/{len(questions[lang])}] {q!r}\n   ⛔ ОШИБКА (попытка {attempt}/3): {exc}")
                        await asyncio.sleep(10)
                if r is None:
                    continue

                tcs = getattr(r, "tool_calls", None) or []
                model_fired = bool(tcs)
                # Та же страховка, что node_faq применяет на первом решении хода.
                rescued_by = None
                if not model_fired and args.salvage:
                    rescued = rescue_tool_call(
                        extract_text_content(r), q, tool_names, allow_intent=True,
                    )
                    if rescued is not None:
                        tcs = [{"name": rescued[0], "args": rescued[1]}]
                        rescued_by = rescued[2]
                fired = bool(tcs)
                if rescued_by and expected == "tool":
                    tool_rescued += 1
                ok = _strict_ok(expected, hint, tcs)
                strict_total += 1
                strict_ok += ok
                usage = extract_token_usage(r)
                if dump:
                    dump.write(json.dumps({
                        "lang": lang, "q": q, "expected": expected, "hint": hint,
                        "run": run + 1, "fired": fired, "strict_ok": ok,
                        "model_fired": model_fired, "rescued_by": rescued_by,
                        "tool_calls": [{"name": t["name"], "args": t.get("args")} for t in tcs],
                        "content": r.content,
                        "finish_reason": (r.response_metadata or {}).get("finish_reason"),
                        "usage": usage, "seconds": round(dt, 2),
                    }, ensure_ascii=False) + "\n")
                    dump.flush()
                p = usage.get("prompt_tokens", 0)
                c = usage.get("completion_tokens", 0)
                tot = usage.get("total_tokens", 0)

                t_sum += dt
                tok_prompt += p
                tok_compl += c
                if expected == "tool":
                    tool_expected += 1
                    if fired:
                        tool_fired += 1

                tag = f"#{run+1} " if args.runs > 1 else ""
                strict_tag = "" if ok else "  ✗ строго"
                print(f"\n[{lang} {i:2}/{len(questions[lang])}] {tag}{q!r}   {_verdict(expected, fired)}{strict_tag}  (ждём: {hint})")
                if fired:
                    calls = ", ".join(f"{c_['name']}({c_.get('args')})" for c_ in tcs)
                    tag_r = f"  [спасено: {rescued_by}]" if rescued_by else ""
                    print(f"   → TOOL  {calls}{tag_r}")
                else:
                    print(f"   → TEXT  {extract_text_content(r)[:160]!r}")
                print(f"   tokens: prompt={p} compl={c} total={tot}   |   {dt:.2f}s")

        n = len(questions[lang]) * args.runs
        rate = f"{tool_fired}/{tool_expected}" if tool_expected else "—"
        avg = t_sum / n if n else 0.0
        grand[lang] = (tool_fired, tool_expected, avg, tok_prompt, tok_compl, strict_ok, strict_total, tool_rescued)
        print(f"\n  ▸ {lang.upper()}: инструменты сработали {rate} (где ждали) | "
              f"строго {strict_ok}/{strict_total} | "
              f"avg {avg:.2f}s | tokens prompt={tok_prompt} compl={tok_compl}")

    print(f"\n{'='*78}\n  СВОДКА  ({model.split('/')[-1]}, temp={args.temp}, набор {args.qset})\n{'='*78}")
    for lang in langs:
        tf, te, avg, tp, tc, so, st, tr = grand[lang]
        rate = f"{tf}/{te}" if te else "—"
        n = len(questions[lang]) * args.runs
        avg_prompt = tp // n if n else 0
        if args.salvage and te:
            rate += f" (сама модель {tf - tr}, спасено {tr})"
        print(f"  {lang.upper()}: tool-routing {rate:>6}  | строго {so}/{st}  | avg {avg:.2f}s "
              f"| prompt ~{avg_prompt} ток/запрос")
    print("  tool-routing — вызван ли инструмент там, где его ждали (метрика отчёта).")
    print("  строго — тот ли инструмент и та ли категория, по всем вопросам набора.")
    if dump:
        dump.close()
        print(f"  Сырые ответы: {args.dump}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
