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
python3 scripts/seed_branches.py

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

### Project Structure

```
app/
├── agent/                    # LangGraph agent (modular)
│   ├── agent.py              # Agent class — entry point, send_message()
│   ├── graph.py              # StateGraph definition (router → faq|calc_flow|human_mode → END)
│   ├── state.py              # BotState TypedDict + AgentTurnResult
│   ├── llm.py                # ChatOpenAI factory + token usage/cost tracking
│   ├── tools.py              # 12 LLM tools (@lc_tool) + _FAQ_TOOLS list
│   ├── constants.py          # CREDIT_SECTION_MAP, contextvars, flow/step names
│   ├── i18n.py               # Agent-level translations (ru/en/uz)
│   ├── intent.py             # Product category detection from text
│   ├── products.py           # Product loading, formatting, matching
│   ├── calc_extractor.py     # Numeric extraction for calculator inputs
│   ├── parsers.py            # Response parsing utilities
│   ├── checkpointer.py       # Checkpoint backend setup (memory/postgres)
│   └── nodes/
│       ├── router.py         # node_router — routes to faq/calc_flow/human_mode
│       ├── faq.py            # node_faq — LLM with tool-calling (max 3 rounds)
│       ├── calc_flow.py      # node_calc_flow — deterministic calculator + lead capture
│       ├── human_mode.py     # node_human_mode — interrupt() for operator handoff
│       └── helpers.py        # Shared node utilities
├── admin/                    # SQLAdmin panel at /admin
│   ├── views.py              # ModelView classes for all models
│   ├── auth.py               # Env-based authentication backend
│   ├── setup.py              # SQLAdmin initialization and mounting
│   ├── dashboard_view.py     # Custom admin dashboard
│   └── dashboard_data.py     # Dashboard data queries
├── api/
│   └── fastapi_app.py        # FastAPI app, webhook, /operator/send, inactivity watcher
├── bot/
│   ├── handlers/commands.py  # Telegram command/message handlers
│   ├── keyboards/            # Reply keyboard builders (common, feedback, human, menu)
│   ├── middlewares/           # aiogram middlewares (chat_service injection)
│   └── i18n.py               # Bot-level translations (ru/en/uz)
├── services/
│   ├── agent_client.py       # Thin wrapper around Agent
│   ├── chat_service.py       # Session lifecycle, message persistence, hybrid mode
│   ├── chat_middleware_client.py  # External chat middleware integration
│   └── telegram_sender.py    # Telegram message sending utility
├── utils/
│   ├── data_loaders.py       # Async DB loaders for products and FAQ
│   ├── faq_tools.py          # FAQ search with text normalization and stemming
│   ├── pdf_generator.py      # PDF amortization schedule generator
│   ├── text_utils.py         # Shared text normalization / stemming
│   └── cbu_rates.py          # CBU exchange rates fetcher
├── db/
│   ├── models.py             # SQLAlchemy ORM models
│   ├── session.py            # Async session factory
│   └── alembic/              # Alembic migrations
├── config.py                 # Dataclass settings with @lru_cache get_settings()
└── data/ai_chat_info/        # Product data JSON files

chat-middleware-mock/          # Mock middleware for testing
scripts/                       # Seed and import scripts
tests/                         # pytest tests
templates/                     # Jinja2 templates (dashboard, sqladmin)
nginx/                         # Nginx config for production
```

### LangGraph Agent (`app/agent/`)

**Graph: `router → faq | calc_flow | human_mode → END`**

4 nodes. The LLM decides intent via tool selection (no separate intent classifier).

#### Router (`nodes/router.py`)

```
human_mode == True       → human_mode
dialog.lead_step set     → calc_flow
dialog.flow == calc_flow → calc_flow
everything else          → faq
```

Uses `Command(goto=...)` for routing — no explicit conditional edges in graph.

#### node_faq (`nodes/faq.py`) — LLM with 12 tools

The LLM receives message history + current state context, then picks which tool to call. Max 3 tool-call rounds per turn.

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
| `back_to_product_list()` | back button / назад |
| `start_calculator()` | рассчитать / подать заявку |
| `faq_lookup(query)` | any banking question |
| `request_operator(reason)` | user wants live operator / identity-required ops |

Tools that need dialog state declare `state: Annotated[dict, InjectedState] = None` — `ToolNode` injects graph state automatically, the parameter stays hidden from the LLM schema. Product categories: `mortgage`, `autoloan`, `microloan`, `education_credit`, `deposit`, `debit_card`, `fx_card`.

#### node_calc_flow (`nodes/calc_flow.py`) — deterministic calculator + lead capture

Two sub-flows:

**calc_step** — collects inputs for payment calculation:
- Credit: amount → term → downpayment → generates PDF schedule
- Deposit: amount → term → text calculation
- If user asks a side question mid-calc, answers it via LLM then re-asks the current step

**lead_step** — captures contact info after calculation:
- offer ("Want us to call?") → name → phone → saves Lead to DB

#### node_human_mode (`nodes/human_mode.py`) — operator handoff

Uses `interrupt()` to pause the graph. Operator responds via `/operator/send` API or SQLAdmin. Reply injected via `Command(resume=...)`.

### State (`BotState` in `app/agent/state.py`)

```python
class BotState(TypedDict):
    messages: List[Any]           # LangChain message history
    last_user_text: str           # current user input
    answer: str                   # bot response
    human_mode: bool              # operator mode flag
    keyboard_options: List[str]   # Telegram reply keyboard buttons
    dialog: dict                  # flow state (see _default_dialog())
    _route: str                   # internal routing target
    session_id: str
    user_id: int
    show_operator_button: bool    # show "connect to operator" button
    token_usage: dict             # LLM token usage + cost tracking
```

The `dialog` dict tracks: `flow`, `category`, `products`, `selected_product`, `calc_step`, `calc_slots`, `lead_step`, `lead_slots`, `fallback_streak`.

### LLM Configuration (`app/agent/llm.py`)

- Uses `langchain-openai` / `ChatOpenAI`
- Default model: `gpt-4o-mini` (override via `OPENAI_MODEL` or `LOCAL_AGENT_INTENT_LLM_MODEL`)
- Supports custom `OPENAI_BASE_URL` for OpenAI-compatible APIs
- Built-in token usage tracking and cost calculation per model

### Hybrid Bot/Operator Mode

- `ChatSession.human_mode = True` → messages saved to DB but NOT forwarded to LangGraph
- Operators respond via Telegram (with `/op` prefix), the `/operator/send` REST API, or the SQLAdmin panel at `/admin`
- Background inactivity watcher (60s interval) auto-returns stale human-mode sessions to bot after `HUMAN_MODE_OPERATOR_TIMEOUT_MINUTES`
- `request_operator` tool allows LLM to trigger handoff for identity-required operations

### Database Models (`app/db/models.py`)

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
- `OPENAI_MODEL` — override LLM model name
- `OPENAI_BASE_URL` — custom OpenAI-compatible API base URL
- `WEBHOOK_BASE_URL` — if set, registers Telegram webhook; otherwise uses polling
- `WEBHOOK_SECRET` — verified on each webhook request
- `OPERATOR_IDS` — comma-separated Telegram IDs allowed to act as operators
- `OPERATOR_API_KEY` — bearer key for `/operator/send` endpoint
- `LANGGRAPH_CHECKPOINT_BACKEND` — `memory|postgres|auto`
- `MAX_DIALOG_MESSAGES` — message history limit (default: 12)
- `SESSION_INACTIVITY_TIMEOUT_MINUTES` (default 60)
- `HUMAN_MODE_OPERATOR_TIMEOUT_MINUTES` (default 10)

## Webhook vs Polling

- Set `WEBHOOK_BASE_URL` in `.env` for webhook mode (required for production)
- Leave empty for long-polling mode (convenient for local dev without ngrok)
- For local webhook testing, use ngrok: `ngrok http 8001`

## Adding New Product Types

1. Add ORM model to `app/db/models.py`
2. Create Alembic migration: `alembic revision -m "..." --autogenerate`
3. Add category to `CREDIT_SECTION_MAP` in `app/agent/constants.py`
4. Add loading logic to `_get_products_by_category()` in `app/agent/products.py`
5. Add translations in `app/agent/i18n.py`
6. Add data loader to `app/utils/data_loaders.py` if needed
7. Create seed script in `scripts/`
8. The LLM will automatically route to `get_products(category)` — no intent registration needed

## Adding New Tools

1. Define an `@lc_tool` async function in `app/agent/tools.py`
2. Add it to `_FAQ_TOOLS` list in `app/agent/tools.py`
3. If the tool changes dialog state, add a handler in `nodes/faq.py` (`_update_dialog_from_tools()`)
4. The LLM will discover the tool via its docstring — write a clear description

## Workflow Rules

- **Plan first**: Before any non-trivial task, present a numbered plan and wait for user approval before implementing.
- **Commit descriptions**: After completing a task, suggest a commit message in Russian (short summary + bullet list of changes). Never run git commit — user commits manually.

## Claude Code Setup

### Custom Agents (`.claude/agents/`)
- `langgraph-backend-engineer` — specialized sub-agent for all LangGraph work (design, implementation, debugging, optimization). Always use this agent for LangGraph-related tasks.

### Custom Commands (`.claude/commands/`)
- `/test` — run project tests
- `/migrate` — create and apply Alembic migrations
- `/seed` — run all seed scripts
- `/check` — syntax check all Python files
