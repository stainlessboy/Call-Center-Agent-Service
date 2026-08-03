#!/usr/bin/env python3
"""scripts/diag_agent.py — РЕАЛЬНОЕ сравнение моделей на полном агенте с БД.

Отличие от `diag_model.py`: там мерилось только РЕШЕНИЕ модели позвать инструмент
(без исполнения). Здесь гоняется **весь граф**: инструменты реально исполняются,
ходят в Postgres, отрабатывает qualify-флоу, FAQ-поиск по базе.

Для каждого вопроса скрипт проходит диалог до конца: если бот задал уточняющий
вопрос с кнопками (qualify-флоу), автоматически жмёт первую кнопку — и так до
3 шагов, пока не появятся реальные продукты из БД.

Что меряется по каждому вопросу:
    • дошёл ли ответ до РЕАЛЬНЫХ данных из БД (названия продуктов / текст FAQ)
    • сколько шагов диалога понадобилось
    • токены и время (суммарно за все шаги)

Модели переключаются в одном процессе через env + сброс lru_cache фабрик LLM.

Использование:
    export DIAG_MODEL_TOKEN='<ключ внутреннего эндпоинта>'
    python3 scripts/diag_agent.py                    # все три модели
    python3 scripts/diag_agent.py --only asaka26b    # одна модель
    python3 scripts/diag_agent.py --lang ru
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

# ── Конфигурации моделей ─────────────────────────────────────────────────────
# Токены в коде не храним: внутренний эндпоинт берёт ключ из DIAG_MODEL_TOKEN,
# OpenAI — из OPENAI_API_KEY (уже есть в .env).
ASAKA_BASE = "https://runai.gpu.uz/asaka-b200-test-23-07-2026-56/gemma-serve"

MODELS = {
    "gpt": {
        "label": "gpt-5.4-mini (OpenAI)",
        "env": {"USE_GPT": "true", "OPENAI_MODEL": "gpt-5.4-mini"},
    },
    "asaka26b": {
        "label": "gemma-4-26b (внутр. B200)",
        "env": {
            "USE_GPT": "false",
            "QWEN_MODEL": "gemma-4-26b",
            "QWEN_BASE_URL": f"{ASAKA_BASE}/v1",
        },
        "needs_token": True,
    },
    "asaka31b": {
        "label": "gemma-4-31b (внутр. B200)",
        "env": {
            "USE_GPT": "false",
            "QWEN_MODEL": "gemma-4-31b",
            "QWEN_BASE_URL": f"{ASAKA_BASE}/url-1/v1",
        },
        "needs_token": True,
    },
}

# ── Вопросы ──────────────────────────────────────────────────────────────────
# kind: "product" — должен дойти до списка продуктов из БД (данные засеяны)
#       "faq"     — должен вернуть ответ из таблицы faq
#       "none"    — приветствие, БД не нужна
#       "gap"     — данные НЕ засеяны (вклады/карты/филиалы) — ожидаем пусто
QUESTIONS = {
    "ru": [
        ("Хочу оформить ипотеку",        "product"),
        ("Покажите ваши автокредиты",    "product"),
        ("Нужен микрозайм",              "product"),
        ("Образовательный кредит есть?", "product"),
        ("Мне нужен кредит",             "product"),
        ("Что такое эскроу-счёт?",       "faq"),
        ("Как заблокировать карту?",     "faq"),
        ("Какие документы нужны для кредита?", "faq"),
        ("Какие вклады у вас есть?",     "gap"),
        ("Здравствуйте!",                "none"),
    ],
    "uz": [
        ("Ipoteka olmoqchiman",             "product"),
        ("Avtokreditlaringizni ko'rsating", "product"),
        ("Mikroqarz kerak",                 "product"),
        ("Ta'lim krediti bormi?",           "product"),
        ("Menga kredit kerak",              "product"),
        ("Eskrou hisob nima?",              "faq"),
        ("Kartani qanday bloklash mumkin?", "faq"),
        ("Kredit uchun qanday hujjatlar kerak?", "faq"),
        ("Qanday omonatlaringiz bor?",      "gap"),
        ("Assalomu alaykum!",               "none"),
    ],
}

MAX_STEPS = 6  # сколько шагов диалога проходим (авто-нажатие первой кнопки)


async def _load_db_reference() -> tuple[list[str], list[str]]:
    """Достаёт из БД названия продуктов и тексты FAQ — эталон «реальных данных»."""
    from sqlalchemy import text as sql
    from app.db.session import AsyncSessionLocal

    async with AsyncSessionLocal() as s:
        products = list(
            (await s.execute(sql("SELECT service_name FROM credit_product_offers"))).scalars().all()
        )
        # В таблице faq ответы хранятся по языкам отдельными колонками.
        faq_answers: list[str] = []
        for col in ("answer_ru", "answer_uz", "answer_en"):
            faq_answers += [
                a for a in (
                    await s.execute(sql(f"SELECT {col} FROM faq WHERE {col} IS NOT NULL"))
                ).scalars().all() if a
            ]
    return products, faq_answers


def _norm(s: str) -> str:
    return " ".join((s or "").split()).strip().lower()


def _hit_products(keyboard: list[str], products: list[str]) -> list[str]:
    """Точное совпадение кнопок с `service_name` из БД.

    Когда агент показывает список продуктов, их названия попадают в кнопки —
    это однозначный признак, что данные реально пришли из базы. Поиск подстрок
    по тексту ответа не годится: названия разделов («Микрозайм») совпадают с
    названиями продуктов и дают ложные срабатывания на меню категорий.
    """
    prod_norm = {_norm(p): p for p in products if p and p.strip()}
    out: list[str] = []
    for kb in keyboard:
        p = prod_norm.get(_norm(kb))
        if p and p not in out:
            out.append(p)
    return out


def _hit_faq(blob: str, faq_answers: list[str]) -> bool:
    """Пришёл ли в ответе дословный фрагмент ответа из таблицы faq."""
    low = blob.lower()
    for a in faq_answers:
        a = (a or "").strip()
        if len(a) >= 40 and a.lower()[:60] in low:
            return True
    return False


def _reset_llm_caches() -> None:
    from app.agent import llm as L
    from app.agent import lang_detect as LD

    L._get_chat_openai.cache_clear()
    LD._get_detector_llm.cache_clear()


async def run_model(key: str, cfg: dict, langs: list[str], products, faq_answers) -> dict:
    # Переключаем провайдера и сбрасываем кэши фабрик LLM.
    for k, v in cfg["env"].items():
        os.environ[k] = v
    if cfg.get("needs_token"):
        tok = os.getenv("DIAG_MODEL_TOKEN")
        if not tok:
            raise SystemExit("Нужен DIAG_MODEL_TOKEN для внутреннего эндпоинта.")
        os.environ["QWEN_API_KEY"] = tok
    os.environ["LANGGRAPH_CHECKPOINT_BACKEND"] = "memory"
    _reset_llm_caches()

    from app.agent import Agent

    agent = Agent()
    await agent.setup(backend="memory")

    print(f"\n{'='*80}\n  МОДЕЛЬ: {cfg['label']}\n{'='*80}")
    stats = {}

    try:
        for lang in langs:
            ok_db = total_db = 0
            t_sum = 0.0
            tok_sum = 0
            print(f"\n─── {lang.upper()} " + "─" * 60)

            for q, kind in QUESTIONS[lang]:
                sid = f"diag-{uuid.uuid4().hex[:10]}"
                blob, steps, q_time, q_tokens = "", 0, 0.0, 0
                found: list[str] = []
                kb_all: list[str] = []
                text_in = q

                for _ in range(MAX_STEPS):
                    t0 = time.perf_counter()
                    try:
                        res = await agent.send_message(session_id=sid, user_id=999001, text=text_in)
                    except Exception as exc:  # noqa: BLE001
                        blob += f" [ОШИБКА: {exc}]"
                        break
                    q_time += time.perf_counter() - t0
                    steps += 1
                    tu = res.token_usage or {}
                    q_tokens += int(tu.get("total_tokens") or 0)
                    blob += " " + (res.text or "")
                    kb = res.keyboard_options or []
                    kb_all += kb

                    found = _hit_products(kb_all, products)
                    if found or not kb:
                        break
                    text_in = kb[0]  # авто-ответ на qualify-вопрос

                t_sum += q_time
                tok_sum += q_tokens

                if kind == "product":
                    total_db += 1
                    got = bool(found)
                    if got:
                        ok_db += 1
                    mark = "✅" if got else "❌"
                    detail = f"продукты из БД: {len(found)} ({found[0][:32]}…)" if found else "продуктов из БД НЕ получил"
                elif kind == "faq":
                    total_db += 1
                    got = _hit_faq(blob, faq_answers)
                    if got:
                        ok_db += 1
                    mark = "✅" if got else "❌"
                    detail = "ответ из таблицы faq" if got else "ответ НЕ из faq (общий ответ LLM)"
                elif kind == "gap":
                    mark = "·"
                    detail = "данные не засеяны — ожидаемо пусто"
                else:
                    mark = "·"
                    detail = "приветствие"

                # tok==0 → LLM не вызывался: сработал детерминированный путь
                # (strict-предчек FAQ или роутер), что само по себе важный факт.
                llm_note = " [без LLM]" if q_tokens == 0 else ""
                print(f"  {mark} {q[:42]:44} шагов={steps} {q_time:5.2f}s tok={q_tokens:6}{llm_note}  {detail}")

            n = len(QUESTIONS[lang])
            stats[lang] = (ok_db, total_db, t_sum / n, tok_sum)
            print(f"  ▸ {lang.upper()}: реальные данные из БД {ok_db}/{total_db} | "
                  f"avg {t_sum/n:.2f}s/вопрос | tokens {tok_sum}")
    finally:
        await agent.aclose()

    return stats


async def main() -> int:
    p = argparse.ArgumentParser(description="Реальное сравнение моделей на полном агенте + БД")
    p.add_argument("--only", choices=list(MODELS), default=None)
    p.add_argument("--lang", choices=["ru", "uz", "both"], default="both")
    args = p.parse_args()

    load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=True)

    products, faq_answers = await _load_db_reference()
    print(f"БД: {len(products)} кредитных продуктов, {len(faq_answers)} FAQ-ответов")

    langs = ["ru", "uz"] if args.lang == "both" else [args.lang]
    keys = [args.only] if args.only else list(MODELS)

    grand = {}
    for k in keys:
        grand[k] = await run_model(k, MODELS[k], langs, products, faq_answers)

    print(f"\n{'='*80}\n  СВОДКА — реальные данные из БД (продукты + FAQ)\n{'='*80}")
    for k in keys:
        lab = MODELS[k]["label"]
        parts = []
        for lang in langs:
            ok, tot, avg, tok = grand[k][lang]
            parts.append(f"{lang.upper()} {ok}/{tot} ({avg:.2f}s, {tok} tok)")
        print(f"  {lab:32} " + " | ".join(parts))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
