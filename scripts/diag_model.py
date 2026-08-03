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


def _verdict(expected: str, fired: bool) -> str:
    if expected == "tool":
        return "✅" if fired else "❌"
    if expected == "none":
        return "✅" if not fired else "⚠️"
    return "·"  # any


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
    parser.add_argument("--lang", choices=["ru", "uz", "both"], default="both")
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
        )
        if not token:
            parser.error(
                f"No token for preset '{preset_name}'. Pass --token, or set "
                f"DIAG_TOKEN_{preset_name.upper()} or DIAG_MODEL_TOKEN in the environment."
            )
        chat = ChatOpenAI(
            model=model, base_url=url, api_key=token,
            temperature=args.temp, max_tokens=args.max_tokens,
            timeout=60.0, max_retries=1,
            extra_body={"chat_template_kwargs": {"enable_thinking": False}},
        )

    bound = chat.bind_tools(_FAQ_TOOLS)

    print("=" * 78)
    print(f" МОДЕЛЬ : {model}")
    print(f" URL    : {url}")
    print(f" temp={args.temp}  max_tokens={args.max_tokens}  runs={args.runs}  tools={len(_FAQ_TOOLS)}")
    print("=" * 78)

    langs = ["ru", "uz"] if args.lang == "both" else [args.lang]
    grand = {}

    for lang in langs:
        policy = get_system_policy(lang, "default")
        sys_msg = SystemMessage(content=policy)
        print(f"\n{'─'*78}\n  ЯЗЫК: {lang.upper()}\n{'─'*78}")

        tool_expected = tool_fired = 0
        t_sum = 0.0
        tok_prompt = tok_compl = 0

        for i, (q, expected, hint) in enumerate(QUESTIONS[lang], 1):
            for run in range(args.runs):
                t0 = time.perf_counter()
                try:
                    r = await bound.ainvoke([sys_msg, HumanMessage(content=q)])
                    dt = time.perf_counter() - t0
                except Exception as exc:  # noqa: BLE001
                    print(f"[{lang} {i:2}/{len(QUESTIONS[lang])}] {q!r}\n   ⛔ ОШИБКА: {exc}")
                    continue

                tcs = getattr(r, "tool_calls", None) or []
                fired = bool(tcs)
                usage = extract_token_usage(r)
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
                print(f"\n[{lang} {i:2}/{len(QUESTIONS[lang])}] {tag}{q!r}   {_verdict(expected, fired)}  (ждём: {hint})")
                if fired:
                    calls = ", ".join(f"{c_['name']}({c_.get('args')})" for c_ in tcs)
                    print(f"   → TOOL  {calls}")
                else:
                    print(f"   → TEXT  {extract_text_content(r)[:160]!r}")
                print(f"   tokens: prompt={p} compl={c} total={tot}   |   {dt:.2f}s")

        n = len(QUESTIONS[lang]) * args.runs
        rate = f"{tool_fired}/{tool_expected}" if tool_expected else "—"
        avg = t_sum / n if n else 0.0
        grand[lang] = (tool_fired, tool_expected, avg, tok_prompt, tok_compl)
        print(f"\n  ▸ {lang.upper()}: инструменты сработали {rate} (где ждали) | "
              f"avg {avg:.2f}s | tokens prompt={tok_prompt} compl={tok_compl}")

    print(f"\n{'='*78}\n  СВОДКА  ({model.split('/')[-1]}, temp={args.temp})\n{'='*78}")
    for lang in langs:
        tf, te, avg, tp, tc = grand[lang]
        rate = f"{tf}/{te}" if te else "—"
        print(f"  {lang.upper()}: tool-routing {rate:>6}  | avg {avg:.2f}s | tokens {tp+tc}")
    print("  Критерий: для запросов продукта/филиала/валюты столбец ждёт ✅ (TOOL).")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
