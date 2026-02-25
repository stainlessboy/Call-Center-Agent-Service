# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

A Telegram-based banking chatbot using **aiogram** (Telegram), **FastAPI** (web), **LangGraph** (AI orchestration), and **SQLAlchemy** (ORM). It provides AI-driven financial product selection (mortgages, auto loans, deposits, cards), FAQ handling, PDF payment schedule generation, and a hybrid bot/operator mode for human takeover.

## Commands

```bash
# Setup
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env  # then fill in BOT_TOKEN, OPENAI_API_KEY, DATABASE_URL

# Run migrations
alembic upgrade head
alembic revision -m "description" --autogenerate  # after model changes

# Start the app (uvicorn on APP_HOST:APP_PORT, default 0.0.0.0:8001)
python3 main.py

# Seed product data
python3 scripts/seed_credit_product_offers.py --replace
python3 scripts/seed_deposit_product_offers.py --replace
python3 scripts/seed_card_product_offers.py --replace
python3 scripts/import_faq_xlsx.py "scripts/FAQ.xlsx" --replace

# Health check
curl http://127.0.0.1:8001/health

# Operator message API
curl -X POST http://127.0.0.1:8001/operator/send \
  -H "X-API-Key: $OPERATOR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"session_id": "...", "text": "Hello", "operator_name": "Ali", "operator_id": 123}'
```

There is no test suite — validate changes by running the app and testing via Telegram or the health endpoint.

## Architecture

### Request Flow

```
Telegram → POST /telegram/webhook → FastAPI → aiogram Dispatcher
  → commands.py handlers → ChatService → AgentClient → LocalAgent (LangGraph)
  → Database (SQLAlchemy async)
```

### Key Files

| File | Role |
|------|------|
| `main.py` | Entry point; starts uvicorn |
| `app/api/fastapi_app.py` | FastAPI app, webhook endpoint, operator API, inactivity watcher task |
| `app/bot/handlers/commands.py` | All Telegram command/message handlers |
| `app/bot/i18n.py` | Translations for ru/en/uz |
| `app/services/chat_service.py` | Session lifecycle, message persistence, hybrid mode logic (~600 lines) |
| `app/services/local_agent.py` | LangGraph state machine, intent classification, product flows (~3600 lines) |
| `app/services/agent_client.py` | Thin wrapper around LocalAgent |
| `app/db/models.py` | SQLAlchemy ORM models |
| `app/config.py` | Pydantic settings with `@lru_cache get_settings()` |

### LangGraph State Machine (`local_agent.py`)

The agent follows this graph:
```
classify_intent → node_route → greeting / qa / pdf / start_flow → flow → END
```

- **classify_intent**: LLM (gpt-4o-mini) classifies into one of ~14 intents
- **node_route**: Heuristic pre-checks (`_is_greeting`, `_is_mortgage_intent`, etc.) then routes
- **qa**: Searches FaqItem table + product catalog JSON files, returns formatted answer
- **start_flow / flow**: Step-by-step data collection for credit products; calls tools like `mortgage_selector`, `auto_loan_selector`, `generate_pdf_payment_schedule`, `bank_kb_search`, `get_branches`, `create_lead`

LangGraph checkpointing backend is configurable: `memory` (dev) | `sqlite` | `postgres` (prod).

### Hybrid Bot/Operator Mode

- `ChatSession.human_mode = True` → messages saved to DB but NOT forwarded to LangGraph
- Operators respond via Telegram (with `/op` prefix) or the `/operator/send` REST API
- Background inactivity watcher (60s interval) auto-returns stale human-mode sessions to bot after `HUMAN_MODE_OPERATOR_TIMEOUT_MINUTES`

### Database Models

Core: `User`, `ChatSession`, `Message`
Products: `CreditProductOffer`, `DepositProductOffer`, `CardProductOffer`
Knowledge: `FaqItem`, `Branch`

Migrations in `app/db/alembic/versions/` — three active versions (init, add noncredit tables, drop bank_services).

## Environment Variables

Required:
- `BOT_TOKEN` — Telegram bot token
- `OPENAI_API_KEY` — OpenAI key (default model: `gpt-4o-mini`)
- `DATABASE_URL` — SQLAlchemy async URL (default: `sqlite+aiosqlite:///./bot.db`)

Important optional:
- `WEBHOOK_BASE_URL` — if set, registers Telegram webhook; otherwise uses polling
- `WEBHOOK_SECRET` — verified on each webhook request
- `OPERATOR_IDS` — comma-separated Telegram IDs allowed to act as operators
- `OPERATOR_API_KEY` — bearer key for `/operator/send` endpoint
- `LANGGRAPH_CHECKPOINT_BACKEND` — `memory|sqlite|postgres|auto`
- `SESSION_INACTIVITY_TIMEOUT_MINUTES` (default 60)
- `HUMAN_MODE_OPERATOR_TIMEOUT_MINUTES` (default 10)

## Webhook vs Polling

- Set `WEBHOOK_BASE_URL` in `.env` for webhook mode (required for production)
- Leave empty for long-polling mode (convenient for local dev without ngrok)
- For local webhook testing, use ngrok: `ngrok http 8001`

## Adding New Product Types

1. Add ORM model to `app/db/models.py`
2. Create Alembic migration: `alembic revision -m "..." --autogenerate`
3. Add selector tool function in `local_agent.py`
4. Register new intent in the LLM classification prompt and `node_route`
5. Add product data JSON under `app/data/ai_chat_info/`
6. Create seed script in `scripts/`
