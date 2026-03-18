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

# Run tests
python3 -m pytest tests/test_agent.py -v

# Health check
curl http://127.0.0.1:8001/health

# Operator message API
curl -X POST http://127.0.0.1:8001/operator/send \
  -H "X-API-Key: $OPERATOR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"session_id": "...", "text": "Hello", "operator_name": "Ali", "operator_id": 123}'
```

## Architecture

### Request Flow

```
Telegram → POST /telegram/webhook → FastAPI → aiogram Dispatcher
  → commands.py handlers → ChatService → AgentClient → Agent (LangGraph)
  → Database (SQLAlchemy async)
```

### Key Files

| File | Role |
|------|------|
| `main.py` | Entry point; starts uvicorn |
| `app/api/fastapi_app.py` | FastAPI app, webhook endpoint, operator API, inactivity watcher task |
| `app/bot/handlers/commands.py` | All Telegram command/message handlers |
| `app/bot/i18n.py` | Translations for ru/en/uz |
| `app/services/chat_service.py` | Session lifecycle, message persistence, hybrid mode logic |
| `app/services/agent.py` | LangGraph state machine with LLM tool-calling |
| `app/services/agent_client.py` | Thin wrapper around Agent |
| `app/tools/data_loaders.py` | Async DB loaders for products and FAQ |
| `app/tools/faq_tools.py` | FAQ search with text normalization and stemming |
| `app/tools/pdf_generator.py` | PDF amortization schedule generator |
| `app/tools/text_utils.py` | Shared text normalization / stemming utilities |
| `app/db/models.py` | SQLAlchemy ORM models |
| `app/config.py` | Dataclass settings with `@lru_cache get_settings()` |
| `app/admin/views.py` | SQLAdmin ModelView classes for all models |
| `app/admin/auth.py` | SQLAdmin authentication backend (env-based) |
| `app/admin/setup.py` | SQLAdmin initialization and mounting |

### LangGraph Agent (`app/services/agent.py`)

**Graph: `router → faq | calc_flow | human_mode → END`**

3 nodes + 1 router. The LLM decides intent via tool selection (no separate intent classifier).

#### Router (3 routes)

```
human_mode == True       → human_mode
dialog.lead_step set     → calc_flow
dialog.flow == calc_flow → calc_flow
everything else          → faq
```

#### node_faq — LLM with 11 tools (bind_tools + ToolNode)

The LLM receives message history + current state context, then picks which tool to call. Max 3 tool-call rounds per turn. After the LLM loop, `_update_dialog_from_tools()` inspects which tools were called and updates `dialog` + `keyboard` accordingly.

| Tool | When LLM calls it |
|------|-------------------|
| `greeting_response()` | привет / hello / салом |
| `thanks_response()` | спасибо / рахмат |
| `get_branch_info()` | question about branch / address |
| `get_currency_info()` | question about exchange rates |
| `show_credit_menu()` | "хочу кредит" without specifying type |
| `get_products(category)` | request for specific product type |
| `select_product(product_name)` | user picks a product from list |
| `compare_products(query)` | compare products |
| `back_to_product_list()` | ◀ button / назад |
| `start_calculator()` | ✅ рассчитать / подать заявку |
| `faq_lookup(query)` | any banking question |

Tools access current dialog state via `_CURRENT_DIALOG` contextvar. Product categories: `mortgage`, `autoloan`, `microloan`, `education_credit`, `deposit`, `debit_card`, `fx_card`.

#### node_calc_flow — deterministic calculator + lead capture

Two sub-flows:

**calc_step** — collects inputs for payment calculation:
- Credit: amount → term → downpayment → generates PDF schedule
- Deposit: amount → term → text calculation
- If user asks a side question mid-calc, answers it via LLM then re-asks the current step

**lead_step** — captures contact info after calculation:
- offer ("Want us to call?") → name → phone → saves Lead to DB

#### node_human_mode — operator handoff

Uses `interrupt()` to pause the graph. Operator responds via `/operator/send` API or SQLAdmin. Reply injected via `Command(resume=...)`.

### State (`BotState`)

```python
class BotState(TypedDict):
    messages: List[Any]           # LangChain message history
    last_user_text: str           # current user input
    answer: str                   # bot response
    human_mode: bool              # operator mode flag
    keyboard_options: List[str]   # Telegram reply keyboard buttons
    dialog: dict                  # flow state (see _default_dialog())
    session_id: str
    user_id: int
```

The `dialog` dict tracks: `flow`, `category`, `products`, `selected_product`, `calc_step`, `calc_slots`, `lead_step`, `lead_slots`.

### Hybrid Bot/Operator Mode

- `ChatSession.human_mode = True` → messages saved to DB but NOT forwarded to LangGraph
- Operators respond via Telegram (with `/op` prefix), the `/operator/send` REST API, or the SQLAdmin panel at `/admin`
- Background inactivity watcher (60s interval) auto-returns stale human-mode sessions to bot after `HUMAN_MODE_OPERATOR_TIMEOUT_MINUTES`

### Database Models

Core: `User`, `ChatSession`, `Message`, `Lead`
Products: `CreditProductOffer`, `DepositProductOffer`, `CardProductOffer`
Knowledge: `FaqItem`, `Branch`

LangGraph checkpointing: `memory` (dev) | `postgres` (prod) — configured via `LANGGRAPH_CHECKPOINT_BACKEND`.

## Environment Variables

Required:
- `BOT_TOKEN` — Telegram bot token
- `OPENAI_API_KEY` — OpenAI key (default model: `gpt-4o-mini`)
- `DATABASE_URL` — SQLAlchemy async URL (default: `postgresql+asyncpg://bankbot:bankbot@localhost:5432/bankbot`)

Admin panel:
- `ADMIN_USERNAME` — admin login (default: `admin`)
- `ADMIN_PASSWORD` — admin password (default: `admin`)
- `ADMIN_SECRET_KEY` — session cookie secret key

Important optional:
- `WEBHOOK_BASE_URL` — if set, registers Telegram webhook; otherwise uses polling
- `WEBHOOK_SECRET` — verified on each webhook request
- `OPERATOR_IDS` — comma-separated Telegram IDs allowed to act as operators
- `OPERATOR_API_KEY` — bearer key for `/operator/send` endpoint
- `LANGGRAPH_CHECKPOINT_BACKEND` — `memory|postgres|auto`
- `LOCAL_AGENT_INTENT_LLM_MODEL` — model for LLM calls (default: `gpt-4o-mini`)
- `OPENAI_BASE_URL` — custom OpenAI-compatible API base URL
- `MAX_DIALOG_MESSAGES` — message history limit (default: 50)
- `SESSION_INACTIVITY_TIMEOUT_MINUTES` (default 60)
- `HUMAN_MODE_OPERATOR_TIMEOUT_MINUTES` (default 10)

## Webhook vs Polling

- Set `WEBHOOK_BASE_URL` in `.env` for webhook mode (required for production)
- Leave empty for long-polling mode (convenient for local dev without ngrok)
- For local webhook testing, use ngrok: `ngrok http 8001`

## Adding New Product Types

1. Add ORM model to `app/db/models.py`
2. Create Alembic migration: `alembic revision -m "..." --autogenerate`
3. Add category to `CREDIT_SECTION_MAP` / `CATEGORY_LABELS` / `CALC_QUESTIONS` in `agent.py`
4. Add loading logic to `_get_products_by_category()` in `agent.py`
5. Add data loader to `app/tools/data_loaders.py` if needed
6. Create seed script in `scripts/`
7. The LLM will automatically route to `get_products(category)` — no intent registration needed

## Adding New Tools

1. Define an `@lc_tool` async function in `agent.py`
2. Add it to `_FAQ_TOOLS` list
3. If the tool changes dialog state, add a handler in `_update_dialog_from_tools()`
4. The LLM will discover the tool via its docstring — write a clear description
