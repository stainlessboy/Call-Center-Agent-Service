"""Диагностика pgvector-поиска по FAQ.

Что проверяет:
  1. Расширение `vector` установлено в Postgres.
  2. Количество FAQ-записей с непустыми эмбеддингами по каждому языку.
  3. Реальный запрос к БД: лексический score, семантический score, гибрид + ответ.

Использование:
    python3 scripts/check_faq_embeddings.py
    python3 scripts/check_faq_embeddings.py "как кредит быстрее погасить"
    python3 scripts/check_faq_embeddings.py --lang en "how to close deposit early"
    python3 scripts/check_faq_embeddings.py --query "..." --lang ru
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv
load_dotenv()

from sqlalchemy import text  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.db.session import get_session  # noqa: E402
from app.utils.faq_tools import (  # noqa: E402
    _faq_lookup_with_score,
    _lexical_lookup,
    _semantic_lookup,
)


DEFAULT_QUERIES = [
    ("ru", "как кредит быстрее погасить"),
    ("ru", "можно ли закрыть вклад досрочно"),
    ("en", "how to block my card"),
    ("uz", "kartani qanday bloklash mumkin"),
]


def _fmt_score(s: float) -> str:
    if s >= 0.70:
        marker = "✓ strong"
    elif s >= 0.50:
        marker = "~ weak"
    else:
        marker = "× noise"
    return f"{s:.3f}  [{marker}]"


def _truncate(s: str | None, n: int = 140) -> str:
    if not s:
        return "<нет ответа>"
    s = " ".join(s.split())
    return s if len(s) <= n else s[: n - 1] + "…"


async def check_extension() -> bool:
    print("─" * 72)
    print("1. Расширение pgvector")
    print("─" * 72)
    async with get_session() as session:
        row = (await session.execute(
            text("SELECT extname, extversion FROM pg_extension WHERE extname = 'vector'")
        )).first()
    if row is None:
        print("  ✗ Расширение `vector` НЕ установлено. Запусти: alembic upgrade head")
        return False
    print(f"  ✓ vector v{row[1]}")
    return True


async def check_coverage() -> int:
    print()
    print("─" * 72)
    print("2. Покрытие эмбеддингами")
    print("─" * 72)
    async with get_session() as session:
        row = (await session.execute(text(
            """
            SELECT
              COUNT(*) AS total,
              COUNT(embedding_ru) AS with_ru,
              COUNT(embedding_en) AS with_en,
              COUNT(embedding_uz) AS with_uz,
              COUNT(question_ru) AS q_ru,
              COUNT(question_en) AS q_en,
              COUNT(question_uz) AS q_uz
            FROM faq
            """
        ))).first()
    total, w_ru, w_en, w_uz, q_ru, q_en, q_uz = row
    print(f"  всего записей FAQ:                {total}")
    print(f"  RU: вопросов {q_ru:>4} | с эмбеддингом {w_ru:>4}  ({_pct(w_ru, q_ru)})")
    print(f"  EN: вопросов {q_en:>4} | с эмбеддингом {w_en:>4}  ({_pct(w_en, q_en)})")
    print(f"  UZ: вопросов {q_uz:>4} | с эмбеддингом {w_uz:>4}  ({_pct(w_uz, q_uz)})")
    if total == 0:
        print("\n  ⚠  Таблица FAQ пуста — загрузи через /admin/seed")
    elif (w_ru + w_en + w_uz) == 0:
        print("\n  ⚠  Эмбеддингов нет ни одного — нажми «Пересчитать эмбеддинги» в /admin/seed")
    return total


def _pct(part: int, total: int) -> str:
    if total == 0:
        return "n/a"
    return f"{100 * part / total:.0f}%"


async def check_query(query: str, lang: str) -> None:
    print()
    print("─" * 72)
    print(f"3. Запрос:  {query!r}   (lang={lang})")
    print("─" * 72)

    lex_answer, lex_score = await _lexical_lookup(query, lang)
    sem_answer, sem_score = await _semantic_lookup(query, lang)
    hybrid_answer, hybrid_score = await _faq_lookup_with_score(query, lang)

    print(f"  lex score:    {_fmt_score(lex_score)}")
    print(f"    answer:     {_truncate(lex_answer)}")
    print()
    print(f"  sem score:    {_fmt_score(sem_score)}")
    print(f"    answer:     {_truncate(sem_answer)}")
    print()
    print(f"  hybrid:       {_fmt_score(hybrid_score)}")
    print(f"    answer:     {_truncate(hybrid_answer)}")

    settings = get_settings()
    if hybrid_score >= settings.faq_sem_strict_threshold:
        verdict = f"BOT покажет ответ (порог STRICT={settings.faq_sem_strict_threshold})"
    elif hybrid_score >= settings.faq_sem_low_threshold:
        verdict = f"WEAK match (между {settings.faq_sem_low_threshold} и {settings.faq_sem_strict_threshold}) — fallback к LLM"
    else:
        verdict = "ниже LOW — fallback ответ «не нашёл»"
    print(f"\n  Вердикт:    {verdict}")


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("query", nargs="?", default=None, help="запрос; если опущен — прогон тестового набора")
    parser.add_argument("--lang", default="ru", choices=["ru", "en", "uz"])
    args = parser.parse_args()

    settings = get_settings()
    print()
    print("═" * 72)
    print(" FAQ pgvector diagnostics")
    print("═" * 72)
    print(f"  feature enabled: {settings.faq_embedding_enabled}")
    print(f"  model:           {settings.faq_embedding_model}")
    print(f"  dim:             {settings.faq_embedding_dim}")
    print(f"  thresholds:      strict={settings.faq_sem_strict_threshold}  low={settings.faq_sem_low_threshold}")
    print()

    if not await check_extension():
        return
    total = await check_coverage()
    if total == 0:
        return

    if args.query:
        await check_query(args.query, args.lang)
    else:
        for lang, q in DEFAULT_QUERIES:
            await check_query(q, lang)

    print()


if __name__ == "__main__":
    asyncio.run(main())
