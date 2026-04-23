"""Локальный CLI-клиент для агента — для быстрой отладки без Telegram/FastAPI.

Использование:
    # REPL: многоходовой диалог в одной сессии
    python3 scripts/chat_cli.py

    # Один запрос и выход
    python3 scripts/chat_cli.py "Филиалларингда навбатсиз хизмат курсатолисизме"

    # С конкретным session_id (чтобы продолжить диалог между запусками)
    python3 scripts/chat_cli.py --session my-test "привет"

    # На боевом PostgreSQL-чекпоинтере (по умолчанию memory — БД бота не трогается)
    python3 scripts/chat_cli.py --backend postgres

В REPL: команда /new — начать новую сессию, /quit — выход.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv


async def _run_one(agent, session_id: str, user_id: int, text: str) -> None:
    res = await agent.send_message(session_id=session_id, user_id=user_id, text=text)
    print(f"\nBOT: {res.text}")
    if res.keyboard_options:
        print(f"[keyboard: {res.keyboard_options}]")
    if res.show_operator_button:
        print("[→ operator button shown]")
    if res.token_usage:
        tu = res.token_usage
        print(
            f"[model={tu.get('model')} "
            f"prompt={tu.get('prompt_tokens')} completion={tu.get('completion_tokens')} "
            f"cost=${tu.get('cost', 0):.6f}]"
        )


async def main() -> int:
    parser = argparse.ArgumentParser(description="CLI клиент для LangGraph агента")
    parser.add_argument(
        "message",
        nargs="?",
        default=None,
        help="Сообщение. Если не задано — запускается REPL.",
    )
    parser.add_argument(
        "--session",
        default=None,
        help="session_id (по умолчанию — свежий uuid).",
    )
    parser.add_argument(
        "--user-id",
        type=int,
        default=999000,
        help="user_id (default 999000).",
    )
    parser.add_argument(
        "--backend",
        choices=("memory", "postgres", "auto"),
        default="memory",
        help="Checkpoint backend. По умолчанию memory — боевую БД не трогает.",
    )
    args = parser.parse_args()

    load_dotenv()
    os.environ["LANGGRAPH_CHECKPOINT_BACKEND"] = args.backend

    from app.agent import Agent

    agent = Agent()
    await agent.setup(backend=args.backend)

    session_id = args.session or f"cli-{uuid.uuid4().hex[:8]}"
    model = os.getenv("OPENAI_MODEL") or "(default from llm.py)"
    print(f"model={model}  backend={args.backend}  session={session_id}")

    try:
        if args.message:
            await _run_one(agent, session_id, args.user_id, args.message)
            return 0

        print("REPL. Команды: /new — новая сессия, /quit — выход.\n")
        while True:
            try:
                text = input("YOU: ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                return 0
            if not text:
                continue
            if text in ("/quit", "/exit"):
                return 0
            if text == "/new":
                session_id = f"cli-{uuid.uuid4().hex[:8]}"
                print(f"[new session={session_id}]")
                continue
            await _run_one(agent, session_id, args.user_id, text)
    finally:
        await agent.aclose()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()) or 0)
