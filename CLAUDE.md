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

# Seed product data, FAQ, and branches:
#   open http://127.0.0.1:8001/admin/seed and upload the xlsx files via the form.
# CLI seed scripts were removed — admin form is the only entry point now.

# Run tests
python3 -m pytest tests/ -v

# Health check
curl http://127.0.0.1:8001/health
```

The only HTTP endpoints are `GET /health` and `POST /telegram/webhook` (plus the SQLAdmin panel at `/admin`). There is no `/operator/send` REST API — operator handoff goes through the Asaka chat-middleware (see Hybrid Bot/Operator Mode below).

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
│   ├── graph.py              # StateGraph definition (router → faq|calc_flow|qualify_flow|human_mode → END)
│   ├── state.py              # BotState TypedDict + AgentTurnResult + _default_dialog()/_reset_dialog()
│   ├── llm.py                # ChatOpenAI factory + token usage/cost tracking
│   ├── tools.py              # 11 LLM tools (@lc_tool) + _FAQ_TOOLS list
│   ├── constants.py          # CREDIT_SECTION_MAP, contextvars, flow/step names
│   ├── i18n.py               # Agent-level translations (ru/en/uz)
│   ├── intent.py             # Product category detection from text
│   ├── products.py           # Product loading, formatting, matching
│   ├── branches.py           # Office (filial/sales office/point) search + card formatting
│   ├── qualify.py            # Qualification decision trees (pre-listing questionnaire)
│   ├── rate_rules.py         # Dynamic credit rate engine (CreditRateRule matching)
│   ├── calc_extractor.py     # Numeric extraction for calculator inputs (LLM-assisted)
│   ├── lang_detect.py        # LLM language detector (ru/en/uz)
│   ├── lang_heuristic.py     # Fast heuristic language hints
│   ├── pii_masker.py         # Regex PII masking before text reaches OpenAI
│   ├── checkpointer.py       # Checkpoint backend setup (memory/postgres)
│   └── nodes/
│       ├── router.py         # node_router — routes to faq/calc_flow/qualify_flow/human_mode
│       ├── faq.py            # node_faq — LLM with tool-calling (max 3 rounds)
│       ├── calc_flow.py      # node_calc_flow — deterministic calculator + lead capture
│       ├── qualify_flow.py   # node_qualify_flow — deterministic qualification questionnaire
│       ├── human_mode.py     # node_human_mode_turn — interrupt() for operator handoff
│       └── helpers.py        # Shared node utilities (_finalize_turn, history trimming)
├── admin/                    # SQLAdmin panel at /admin
│   ├── views.py              # ModelView classes for all models
│   ├── auth.py               # Env-based authentication backend
│   ├── setup.py              # SQLAdmin initialization and mounting
│   ├── dashboard_view.py     # Custom admin dashboard
│   ├── dashboard_data.py     # Dashboard data queries
│   ├── seed_view.py          # /admin/seed — upload xlsx → DB
│   └── services/             # Excel parsing + DB seeding (called by seed_view.py)
│       ├── products_excel.py # AI CHAT INFO.xlsx → JSON manifest
│       ├── credit_seed.py    # JSON → CreditProductOffer
│       ├── deposit_seed.py   # JSON → DepositProductOffer
│       ├── card_seed.py      # JSON → CardProductOffer
│       ├── faq_import.py     # FAQ xlsx → FaqItem
│       └── branches_seed.py  # 3 xlsx → Filial/SalesOffice/SalesPoint
├── api/
│   └── fastapi_app.py        # FastAPI app, webhook, /health, middleware callbacks, inactivity watcher
├── bot/
│   ├── handlers/commands.py  # Telegram command/message handlers
│   ├── keyboards/            # Reply keyboard builders (common, feedback, human, menu)
│   ├── middlewares/          # aiogram middlewares (chat_service injection, rate limit)
│   ├── links.py              # External link constants
│   └── i18n.py               # Bot-level translations (ru/en/uz)
├── services/
│   ├── agent_client.py       # Thin wrapper around Agent
│   ├── chat_service.py       # Session lifecycle, message persistence, hybrid mode
│   ├── chat_middleware_client.py  # Asaka chat-middleware integration (JWT auth, Socket.IO)
│   ├── middleware_registry.py # Process-wide handle to ChatMiddlewareClient (set in lifespan)
│   ├── middleware_files.py   # MinIO upload + download/forward operator media to Telegram
│   └── telegram_sender.py    # Telegram message sending utility
├── utils/
│   ├── data_loaders.py       # Async DB loaders for products and FAQ
│   ├── faq_tools.py          # Hybrid FAQ search: lexical + pgvector semantic (tri-tier)
│   ├── pdf_generator.py      # PDF amortization schedule generator
│   ├── text_utils.py         # Shared text normalization / stemming
│   └── cbu_rates.py          # CBU exchange rates fetcher
├── db/
│   ├── models.py             # SQLAlchemy ORM models
│   ├── session.py            # Async session factory
│   ├── events.py             # SQLAlchemy events (FAQ embedding recompute on change)
│   └── alembic/              # Alembic migrations
└── config.py                 # Dataclass settings with @lru_cache get_settings()

chat-middleware-mock/          # Mock middleware for testing
scripts/                       # Dev tools (chat_cli.py — local agent REPL)
tests/                         # pytest tests
templates/                     # Jinja2 templates (dashboard, sqladmin)
nginx/                         # Nginx config for production
```

### LangGraph Agent (`app/agent/`)

**Graph: `router → faq | calc_flow | qualify_flow | human_mode → END`**

5 nodes. The LLM decides intent via tool selection (no separate intent classifier).

#### Router (`nodes/router.py`)

```
human_mode == True                        → human_mode
dialog.lead_step set                      → calc_flow
dialog.flow == calc_flow                  → calc_flow
dialog.flow == qualify_flow               → qualify_flow
calc button + flow == product_detail      → calc_flow (deterministic, no LLM)
everything else                           → faq
```

Uses `Command(goto=...)` for routing — no explicit conditional edges in graph.

#### node_faq (`nodes/faq.py`) — LLM with 11 tools

The LLM receives message history + current state context, then picks which tool to call. Max 3 tool-call rounds per turn (`ToolNode` instance is module-level: `_FAQ_TOOL_NODE`).

| Tool | When LLM calls it |
|------|-------------------|
| `find_office(office_type, query)` | question about branch / office / address |
| `select_office(office_name)` | user picks an office from the shown list |
| `get_office_types_info()` | difference between filial / sales office / sales point |
| `get_currency_info()` | question about exchange rates |
| `show_credit_menu()` | "хочу кредит" without specifying type |
| `get_products(category)` | request for specific product type |
| `select_product(product_name)` | user picks a product from list |
| `start_calculator()` | рассчитать / подать заявку for the selected product |
| `custom_loan_calculator(amount, term, rate)` | generic annuity calc with user's own numbers |
| `faq_lookup(query)` | any banking question |
| `request_operator(reason)` | user wants live operator / identity-required ops (last resort) |

(`clarify` exists in `tools.py` but is commented out of `_FAQ_TOOLS` — temporarily disabled.)

Tools that need dialog state declare `state: Annotated[dict, InjectedState] = None` — `ToolNode` injects graph state automatically, the parameter stays hidden from the LLM schema. Product categories: `mortgage`, `autoloan`, `microloan`, `education_credit`, `deposit`, `debit_card`, `fx_card`.

`faq_lookup` is a hybrid search over the `faq` table: lexical (difflib/token overlap) + semantic (pgvector cosine over `text-embedding-3-small` embeddings), each leg mapped to a tri-tier confidence (strict / low / none). On `strict` the answer is returned as-is; on `low` the closest FAQ questions are surfaced to the LLM as candidates (`FAQ_SEM_TOP_K`).

#### node_qualify_flow (`nodes/qualify_flow.py`) — deterministic qualification questionnaire

Pre-listing questionnaire (decision trees in `app/agent/qualify.py`): before showing products, the user answers a few button questions (e.g. deposit currency, credit purpose); answers filter the DB query. Terminal nodes either render the filtered product list (re-entering the normal `select_product` → calculator chain) or show a dead-end message. Side questions mid-questionnaire are answered via LLM, then the current question is re-asked.

#### node_calc_flow (`nodes/calc_flow.py`) — deterministic calculator + lead capture

Two sub-flows:

**calc_step** — collects inputs for payment calculation:
- Credit: amount → term → downpayment → generates PDF schedule
- Deposit: amount → term → text calculation
- If user asks a side question mid-calc, answers it via LLM then re-asks the current step

**lead_step** — captures contact info after calculation:
- offer ("Want us to call?") → name → phone → saves Lead to DB

#### node_human_mode_turn (`nodes/human_mode.py`) — operator handoff

Uses `interrupt()` to pause the graph. Operator replies arrive via the Asaka chat-middleware Socket.IO callback (`_on_agent_message` in `fastapi_app.py`) and are injected via `Command(resume=...)` (`agent_client.resume_human_mode`).

### State (`BotState` in `app/agent/state.py`)

```python
class BotState(TypedDict):
    messages: List[Any]           # LangChain message history
    last_user_text: str           # current user input
    answer: str                   # bot response
    human_mode: bool              # operator mode flag
    keyboard_options: List[str]   # Telegram reply keyboard buttons
    dialog: dict                  # flow state (see _default_dialog())
    lang: str                     # "ru" | "en" | "uz" — set by the language detector in agent._ainvoke
    _route: str                   # internal routing target
    session_id: str
    user_id: int
    show_operator_button: bool    # show "connect to operator" button
    token_usage: dict             # LLM token usage + cost tracking
```

The `dialog` dict tracks: `flow`, `category`, `products`, `selected_product`, `calc_step`, `calc_slots`, `lead_step`, `lead_slots`, `qualify_category`, `qualify_node`, `qualify_answers`, `fallback_streak`, `last_lang`, `offices`, `selected_office`, `office_type`. Sticky session flags (e.g. `lang_switch_offered`) survive dialog resets — use `_reset_dialog()` instead of bare `_default_dialog()` when resetting mid-session; `_finalize_turn()` also carries them over.

### LLM Configuration (`app/agent/llm.py`)

- Uses `langchain-openai` / `ChatOpenAI`
- Default model: `gpt-4o-mini` (override via `OPENAI_MODEL` or `LOCAL_AGENT_INTENT_LLM_MODEL`)
- Supports custom `OPENAI_BASE_URL` for OpenAI-compatible APIs
- Built-in token usage tracking and cost calculation per model

### Hybrid Bot/Operator Mode

- `ChatSession.human_mode = True` → user messages are saved to DB and forwarded to the chat-middleware (or routed through the LangGraph `interrupt()` if no middleware chat is active); they are NOT processed by the LLM
- Handoff is triggered by the inline «Живой оператор» button (callbacks `human:<sid>` / `bot:<sid>`) or by the `request_operator` tool (LLM decides, e.g. identity-required operations)
- Operators work in the Asaka chat-middleware; their replies come back over Socket.IO and are delivered to Telegram + resumed into the graph
- The `ChatMiddlewareClient` instance is registered in `app/services/middleware_registry.py` during app startup — get it via `get_middleware_client()`, never import the FastAPI app from services/handlers
- Background inactivity watcher (60s interval) auto-returns stale human-mode sessions to bot after `HUMAN_MODE_OPERATOR_TIMEOUT_MINUTES`
- **Asaka chat-middleware integration** (Socket.IO + JWT) — full protocol details, sequence diagrams, env vars and run checklist in [docs/CHAT_MIDDLEWARE_INTEGRATION.md](docs/CHAT_MIDDLEWARE_INTEGRATION.md)

### Database Models (`app/db/models.py`)

Core: `User`, `ChatSession`, `Message`, `Lead`
Products: `CreditProductOffer` (+ `CreditRateRule` — per-product dynamic rate rules), `DepositProductOffer`, `CardProductOffer`
Knowledge: `FaqItem` (with per-language pgvector embedding columns), offices: `Filial`, `SalesOffice`, `SalesPoint`

FAQ embeddings are recomputed automatically on insert/update via SQLAlchemy events (`app/db/events.py`); the Postgres image must provide the pgvector extension (`pgvector/pgvector:pg16` in docker-compose).

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

App / network:
- `APP_HOST` / `APP_PORT` — uvicorn bind address (defaults `0.0.0.0:8001`)
- `LOG_LEVEL` (default `INFO`)
- `FORWARDED_ALLOW_IPS` — proxies trusted for `X-Forwarded-*` headers (default `127.0.0.1`)
- `WEBHOOK_BASE_URL` — if set, registers Telegram webhook; otherwise uses polling
- `WEBHOOK_PATH` (default `/telegram/webhook`)
- `WEBHOOK_SECRET` — verified (timing-safe) on each webhook request
- `MAX_MESSAGE_LENGTH` (default `4000`), `DAILY_MESSAGE_LIMIT` (default `30`), `RATE_LIMIT_PER_MINUTE` (default `20`)
- `DB_POOL_SIZE` (default `10`) / `DB_POOL_MAX_OVERFLOW` (default `20`)

LLM:
- `OPENAI_MODEL` — override LLM model name
- `OPENAI_BASE_URL` — custom OpenAI-compatible API base URL
- `OPENAI_REQUEST_TIMEOUT` — per-request timeout for the main LLM (seconds, default `15`)
- `OPENAI_MAX_RETRIES` — retries for all OpenAI calls (default `1`)
- `AGENT_TIMEOUT_SECONDS` — per-turn timeout for agent invocation (seconds, default `25`)
- `MAX_DIALOG_TOKENS` — approximate token budget for dialog history sent to the LLM (default `3000`)
- `LANG_DETECTOR_MODEL` — model used by the dedicated language detector (default: `gpt-4o-mini`)
- `LANG_DETECTOR_TIMEOUT` — per-request timeout for the language detector (seconds, default `10`)
- `DEFAULT_CUSTOM_LOAN_RATE_PCT` — fallback rate for calculator when product rate is unavailable (default `20.0`)

LangGraph persistence:
- `LANGGRAPH_CHECKPOINT_BACKEND` — `memory|postgres|auto`
- `LANGGRAPH_CHECKPOINT_URL` — separate URL for the checkpoint DB (defaults to `DATABASE_URL`)
- `LANGGRAPH_DIALOG_TTL_MINUTES` (default `720`)
- `REQUIRE_PERSISTENT_CHECKPOINTER` — `true` in prod/k8s. When set, the app fails fast at startup if the checkpointer resolves to `MemorySaver`, and `/health` returns 503 in the same condition. Without it, `auto` silently degrades to in-memory on DB outage and loses all session state on restart.

Sessions / operator mode:
- `SESSION_INACTIVITY_TIMEOUT_MINUTES` (default `1440`)
- `HUMAN_MODE_OPERATOR_TIMEOUT_MINUTES` (default `10`)
- `MIDDLEWARE_ENABLED`, `MIDDLEWARE_URL`, `MIDDLEWARE_LOGIN`, `MIDDLEWARE_PASSWORD`, `MIDDLEWARE_IS_TEST_REQUEST`, `MIDDLEWARE_VERIFY_SSL`, `MIDDLEWARE_NGINX_WS_URL`, `MIDDLEWARE_WORKING_HOURS_*` — Asaka chat-middleware (see docs/CHAT_MIDDLEWARE_INTEGRATION.md)
- `MINIO_BASE_URL`, `MINIO_USERNAME`, `MINIO_PASSWORD` — media forwarding to operators

FAQ search:
- `FAQ_EMBEDDING_ENABLED` (default `true`), `FAQ_EMBEDDING_MODEL` (default `text-embedding-3-small`), `FAQ_EMBEDDING_DIM` (default `1536`)
- `FAQ_SEM_STRICT_THRESHOLD` / `FAQ_SEM_LOW_THRESHOLD` — semantic (embedding cosine) FAQ tiers (defaults `0.60` / `0.45`)
- `FAQ_LEX_STRICT_THRESHOLD` / `FAQ_LEX_LOW_THRESHOLD` — lexical FAQ tiers (defaults `0.75` / `0.55`); legacy `FAQ_STRICT_THRESHOLD`/`FAQ_LOW_CONFIDENCE_THRESHOLD` are ignored
- `FAQ_SEM_TOP_K` — semantic candidates surfaced to the LLM on low confidence (default `3`)

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
7. Add a seeding service in `app/admin/services/` (parses Excel → writes to DB) and wire it from `app/admin/seed_view.py`
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
- `/seed` — prints instructions for seeding via `/admin/seed` (no CLI scripts)
- `/check` — syntax check all Python files
