from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv
load_dotenv()

from app.agent.tools import faq_lookup  # noqa: E402


async def call_faq_lookup(query: str, lang: str) -> str:
    return await faq_lookup.ainvoke({
        "query": query,
        "state": {"lang": lang, "dialog": {"last_lang": lang}},
    })


async def main() -> None:
    _ = ["ru", "en", "uz"]
    print(await call_faq_lookup("что такое эскроу", "ru"))
    print(await call_faq_lookup("как работает эскроу", "ru"))
    print(await call_faq_lookup("qanaqa ishlide eskrou", "uz"))
    print(await call_faq_lookup("eskrou xakida info kerak", "uz"))


if __name__ == "__main__":
    asyncio.run(main())
