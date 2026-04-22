#!/usr/bin/env python3
"""Compare gpt-4o-mini vs gpt-5-mini (reasoning_effort=minimal) on real UZ user messages.

Pulls the last N user messages from sessions where User.language == 'uz',
runs each message through both models with the production SYSTEM_POLICY,
and prints side-by-side replies with timing, cost and a Cyrillic-leak warning.

Usage:
  python3 scripts/compare_models_uz.py --limit 10
  python3 scripts/compare_models_uz.py --limit 5 --out report.txt
"""
from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from langchain_core.messages import HumanMessage, SystemMessage  # noqa: E402
from langchain_openai import ChatOpenAI  # noqa: E402
from sqlalchemy import desc, select  # noqa: E402

from app.agent.i18n import SYSTEM_POLICY  # noqa: E402
from app.agent.llm import calculate_cost  # noqa: E402
from app.db.models import ChatSession, Message, User  # noqa: E402
from app.db.session import get_session  # noqa: E402

_CYRILLIC = re.compile(r"[А-Яа-яЁёЎўҚқҒғҲҳҶҷ]")


async def fetch_uz_user_messages(limit: int) -> list[str]:
    """Last N user-role messages from UZ sessions, newest first."""
    async with get_session() as s:
        stmt = (
            select(Message.text)
            .join(ChatSession, Message.session_id == ChatSession.id)
            .join(User, ChatSession.user_id == User.id)
            .where(User.language == "uz", Message.role == "user")
            .where(Message.text.is_not(None))
            .order_by(desc(Message.created_at))
            .limit(limit)
        )
        result = await s.execute(stmt)
        return [t for (t,) in result.all() if t and t.strip()]


def _build_llm(model: str, use_reasoning_effort: bool) -> ChatOpenAI:
    kwargs: dict[str, Any] = {
        "model": model,
        "temperature": 0.3,
        "max_tokens": 400,
        "api_key": os.getenv("OPENAI_API_KEY"),
    }
    base = os.getenv("OPENAI_BASE_URL")
    if base:
        kwargs["base_url"] = base
    if use_reasoning_effort:
        kwargs["model_kwargs"] = {"reasoning_effort": "minimal"}
    return ChatOpenAI(**kwargs)


async def call(llm: ChatOpenAI, model_name: str, user_text: str) -> dict:
    start = time.time()
    try:
        ai = await llm.ainvoke([
            SystemMessage(content=SYSTEM_POLICY),
            HumanMessage(content=user_text),
        ])
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}", "elapsed": time.time() - start}

    elapsed = time.time() - start
    content = str(getattr(ai, "content", "") or "")
    meta = getattr(ai, "response_metadata", None) or {}
    usage = meta.get("token_usage") or meta.get("usage") or {}
    cost = calculate_cost(usage, model_name) if usage else 0.0
    return {
        "ok": True,
        "text": content,
        "elapsed": elapsed,
        "usage": usage,
        "cost": cost,
        "has_cyrillic": bool(_CYRILLIC.search(content)),
    }


def _truncate(s: str, n: int) -> str:
    s = s.replace("\n", " ").strip()
    return s if len(s) <= n else s[: n - 1] + "…"


async def main(limit: int, out_path: Path | None) -> None:
    if not os.getenv("OPENAI_API_KEY"):
        print("ERROR: OPENAI_API_KEY is not set.", file=sys.stderr)
        sys.exit(1)

    msgs = await fetch_uz_user_messages(limit)
    if not msgs:
        print("No UZ user messages found. Check User.language == 'uz' in DB.")
        return

    llm_4o = _build_llm("gpt-4o-mini", use_reasoning_effort=False)
    llm_5 = _build_llm("gpt-5-mini", use_reasoning_effort=True)

    lines: list[str] = []
    def out(line: str = "") -> None:
        print(line)
        lines.append(line)

    out(f"Comparing {len(msgs)} UZ messages: gpt-4o-mini vs gpt-5-mini (reasoning=minimal)")
    out("=" * 88)

    total = {"4o": {"time": 0.0, "cost": 0.0, "cyr": 0, "err": 0},
             "5m": {"time": 0.0, "cost": 0.0, "cyr": 0, "err": 0}}

    for i, text in enumerate(msgs, 1):
        out(f"\n[{i}/{len(msgs)}] User (UZ): {_truncate(text, 120)}")
        out("-" * 88)

        r4 = await call(llm_4o, "gpt-4o-mini", text)
        r5 = await call(llm_5, "gpt-5-mini", text)

        # gpt-4o-mini block
        if r4["ok"]:
            flag = " ⚠️CYRILLIC" if r4["has_cyrillic"] else ""
            out(f"  gpt-4o-mini  {r4['elapsed']:5.2f}s  ${r4['cost']:.5f}{flag}")
            out(f"    {_truncate(r4['text'], 280)}")
            total["4o"]["time"] += r4["elapsed"]
            total["4o"]["cost"] += r4["cost"]
            if r4["has_cyrillic"]:
                total["4o"]["cyr"] += 1
        else:
            out(f"  gpt-4o-mini  ERROR: {r4['error']}")
            total["4o"]["err"] += 1

        # gpt-5-mini block
        if r5["ok"]:
            flag = " ⚠️CYRILLIC" if r5["has_cyrillic"] else ""
            out(f"  gpt-5-mini   {r5['elapsed']:5.2f}s  ${r5['cost']:.5f}{flag}")
            out(f"    {_truncate(r5['text'], 280)}")
            total["5m"]["time"] += r5["elapsed"]
            total["5m"]["cost"] += r5["cost"]
            if r5["has_cyrillic"]:
                total["5m"]["cyr"] += 1
        else:
            out(f"  gpt-5-mini   ERROR: {r5['error']}")
            total["5m"]["err"] += 1

    out()
    out("=" * 88)
    out("SUMMARY")
    out(f"  gpt-4o-mini:  avg {total['4o']['time'] / max(len(msgs) - total['4o']['err'], 1):.2f}s  "
        f"total ${total['4o']['cost']:.4f}  cyrillic-leaks: {total['4o']['cyr']}  errors: {total['4o']['err']}")
    out(f"  gpt-5-mini:   avg {total['5m']['time'] / max(len(msgs) - total['5m']['err'], 1):.2f}s  "
        f"total ${total['5m']['cost']:.4f}  cyrillic-leaks: {total['5m']['cyr']}  errors: {total['5m']['err']}")

    if out_path:
        out_path.write_text("\n".join(lines), encoding="utf-8")
        print(f"\nReport saved → {out_path}")


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--limit", type=int, default=10, help="How many UZ user messages to test (default 10)")
    p.add_argument("--out", type=Path, help="Optional path to save the full report as plain text")
    args = p.parse_args()
    asyncio.run(main(args.limit, args.out))
