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

HTTP endpoints: `GET /health`, `POST /telegram/webhook`, the SQLAdmin panel at `/admin`, and the Telegram Mini App (`/api/miniapp/*` + the SPA at `/app` — see [docs/MINIAPP.md](docs/MINIAPP.md)). There is no `/operator/send` REST API — operator handoff goes through the Asaka chat-middleware (see Hybrid Bot/Operator Mode below).

```bash
# Telegram Mini App (frontend/)
make miniapp-install   # npm install
make miniapp-dev       # Vite dev server on :5173, proxies /api to :8001
make miniapp-build     # build into frontend/dist, served by FastAPI at /app
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
│   ├── graph.py              # StateGraph definition (router → faq|calc_flow|qualify_flow|human_mode|recap → END)
│   ├── state.py              # BotState TypedDict + AgentTurnResult + _default_dialog()/_reset_dialog()
│   ├── llm.py                # ChatOpenAI factory + token usage/cost tracking
│   ├── tools.py              # 16 LLM tools (@lc_tool) + _FAQ_TOOLS list
│   ├── constants.py          # CREDIT_SECTION_MAP, contextvars, flow/step names
│   ├── i18n.py               # Agent-level translations (ru/en/uz)
│   ├── intent.py             # Product category detection from text
│   ├── products.py           # Product loading, formatting, matching
│   ├── branches.py           # Office (filial/sales office/point) search + card formatting
│   ├── qualify.py            # Qualification decision trees (pre-listing questionnaire)
│   ├── rate_rules.py         # Dynamic credit rate engine (CreditRateRule matching) + resolve_effective_rate
│   ├── recommend.py          # Deterministic product-ranking engine for recommend_product + dead-end rescue
│   ├── profile.py            # UserProfile load/merge ("personal consultant" memory)
│   ├── memory_extract.py     # Background LLM extractor — facts + relationship recap
│   ├── handoff.py            # Operator handoff summary (LLM briefing sent to the middleware chat)
│   ├── faq_rephrase.py       # Rewords a verbatim FAQ DB answer (both confident-hit paths in node_faq)
│   ├── calc_extractor.py     # Numeric extraction for calculator inputs (LLM-assisted)
│   ├── lang_detect.py        # LLM language detector (ru/en/uz)
│   ├── lang_heuristic.py     # Fast heuristic language hints
│   ├── pii_masker.py         # Regex PII masking before text reaches OpenAI
│   ├── ui_blocks.py          # Mini App calc_result block builders (tools.py + calc_flow.py)
│   ├── streaming.py          # Mini App token-streaming contextvar (node_faq only)
│   ├── checkpointer.py       # Checkpoint backend setup (memory/postgres)
│   └── nodes/
│       ├── router.py         # node_router — routes to faq/calc_flow/qualify_flow/human_mode/recap
│       ├── faq.py            # node_faq — LLM with tool-calling (max 5 rounds)
│       ├── calc_flow.py      # node_calc_flow — deterministic calculator + lead capture
│       ├── qualify_flow.py   # node_qualify_flow — deterministic qualification questionnaire
│       ├── human_mode.py     # node_human_mode_turn — interrupt() for operator handoff
│       ├── recap.py          # node_recap — offer/resume a stale calc_flow/qualify_flow
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
├── miniapp/                  # Telegram Mini App backend (see docs/MINIAPP.md)
│   ├── auth.py               # initData HMAC verification + FastAPI dependency
│   ├── deps.py               # ChatService / DB-user dependencies
│   ├── serializers.py        # Domain dicts → JSON (product ids, bounds, offices)
│   ├── hub.py                # session_id → WebSockets fan-out for operator events
│   ├── static.py             # Serves frontend/dist at /app with SPA fallback
│   └── routes/               # bootstrap, catalog, calc, leads, branches, misc, chat
├── utils/
│   ├── data_loaders.py       # Async DB loaders for products and FAQ
│   ├── faq_tools.py          # Hybrid FAQ search: lexical + pgvector semantic (tri-tier)
│   ├── amortization.py       # Annuity math shared by PDF, bot calculator, Mini App
│   ├── pdf_generator.py      # PDF amortization schedule generator
│   ├── text_utils.py         # Shared text normalization / stemming
│   ├── working_hours.py      # Operator working-hours window (bot + Mini App)
│   └── cbu_rates.py          # CBU exchange rates fetcher
├── db/
│   ├── models.py             # SQLAlchemy ORM models
│   ├── session.py            # Async session factory
│   ├── events.py             # SQLAlchemy events (FAQ embedding recompute on change)
│   └── alembic/              # Alembic migrations
└── config.py                 # Dataclass settings with @lru_cache get_settings()

frontend/                      # Telegram Mini App SPA (React + TS + Vite + zustand)
design_handoff_asaka_miniapp/  # Design reference for the Mini App (HTML prototype)
chat-middleware-mock/          # Mock middleware for testing
scripts/                       # Dev tools (chat_cli.py — local agent REPL)
tests/                         # pytest tests
templates/                     # Jinja2 templates (dashboard, sqladmin)
nginx/                         # Nginx config for production
```

### Telegram Mini App (`app/miniapp/` + `frontend/`)

The Mini App is a **presentation layer only**: every route calls the same functions
the LangGraph nodes call (products, qualification trees, rate rules, amortization,
office search, CBU rates, `ChatService`). Never duplicate a business rule there —
if a Mini App screen needs data the bot computes inline, extract the pure function
and call it from both. Full contract, endpoint list and known gaps:
[docs/MINIAPP.md](docs/MINIAPP.md).

### LangGraph Agent (`app/agent/`)

**Graph: `router → faq | calc_flow | qualify_flow | human_mode | recap → END`**

6 nodes. The LLM decides intent via tool selection (no separate intent classifier).

#### Router (`nodes/router.py`)

```
human_mode == True                        → human_mode
dialog.recap_pending == True              → recap (interpret answer to a pending recap offer)
dialog.flow in (calc_flow, qualify_flow)
  AND stale (RECAP_GAP_MINUTES since
  dialog.last_turn_at)                    → recap (offer to resume)
dialog.lead_step set                      → calc_flow
dialog.flow == calc_flow                  → calc_flow
dialog.flow == qualify_flow               → qualify_flow
calc button + flow == product_detail      → calc_flow (deterministic, no LLM)
everything else                           → faq
```

Uses `Command(goto=...)` for routing — no explicit conditional edges in graph. `dialog.lead_step` always carries `flow == calc_flow` (see `calc_flow.py`), so the recap staleness check above already covers lead-capture turns without a separate check.

#### node_faq (`nodes/faq.py`) — LLM with 16 tools

The LLM receives message history + current state context, then picks which tool to call. Max 5 tool-call rounds per turn (`ToolNode` instance is module-level: `_FAQ_TOOL_NODE`).

| Tool | When LLM calls it |
|------|-------------------|
| `find_office(office_type, query)` | question about branch / office / address |
| `select_office(office_name)` | user picks an office from the shown list |
| `get_office_types_info()` | difference between filial / sales office / sales point |
| `get_currency_info()` | question about exchange rates |
| `show_credit_menu()` | "хочу кредит" without specifying type |
| `get_products(category)` | request for specific product type |
| `select_product(product_name)` | user picks a product from list |
| `compare_products(product_names)` | customer names/picks 2-4 products to compare |
| `start_calculator()` | рассчитать / подать заявку for the selected product |
| `custom_loan_calculator(amount, term, rate)` | generic annuity calc with user's own numbers |
| `what_if_scenario(amount, term_months, downpayment_pct, product_name)` | hypothetical recalculation ("what if the term were longer") — never mutates `dialog.calc_slots` |
| `affordability_check(monthly_payment, loan_amount, term_months)` | "can I afford this payment" — compares against `state.user_profile.facts.income_monthly` when known |
| `faq_lookup(query)` | any banking question |
| `request_operator(reason)` | user wants live operator / identity-required ops (last resort) |
| `clarify(question, options)` | genuinely ambiguous request with 2-4 concrete branches and no other signal to pick one |
| `recommend_product(goal)` | customer describes a life/financial goal without naming a product — ranks via `recommend.py`, best-1 + alternative |

`clarify`'s `options` become reply buttons/chips (`_update_dialog_from_tools`) — a tap returns the exact option text, never free text. An anti-loop guard in `node_faq`'s tool-call round loop (`dialog["clarify_last_turn"]`) makes it physically impossible for the model to call `clarify` twice in a row: if the previous finalized turn was itself a clarify prompt, `clarify` is stripped from the tools bound for the rest of that turn's rounds the moment the model tries it again, forcing a fall-through to `faq_lookup` / a direct answer instead.

Tools that need dialog state declare `state: Annotated[dict, InjectedState] = None` — `ToolNode` injects graph state automatically, the parameter stays hidden from the LLM schema. Product categories: `mortgage`, `autoloan`, `microloan`, `education_credit`, `deposit`, `debit_card`, `fx_card`.

**Mini App structured data (Phase 3, extended Phase 4)**: `find_office`, `select_office`, `get_currency_info`, `get_products`, `select_product`, `compare_products`, `custom_loan_calculator`, `what_if_scenario`, `affordability_check`, `recommend_product` use `response_format="content_and_artifact"` — they return `(text, artifact | None)`, where `artifact` is a `{"type": ..., "data": ...}` UI block. `ToolNode` puts it on `ToolMessage.artifact`, never in `.content`, so it never reaches the LLM's context. `node_faq` accumulates every round's non-`None` artifacts into `state.ui_blocks`; `nodes/calc_flow.py` and `nodes/qualify_flow.py` build their own blocks directly (deterministic nodes, no tool call). Full JSON schema per block type, WS streaming events, and the persistence decision: [docs/MINIAPP.md "UI blocks"](docs/MINIAPP.md#ui-blocks).

`faq_lookup` is a hybrid search over the `faq` table: lexical (difflib/token overlap, with question-frame/filler words like "что"/"такое"/"хочу"/"давайте" stripped from the token-overlap leg via `token_set_content()` in `app/utils/text_utils.py` — natural paraphrases score much closer to the matching FAQ question than a bare keyword query would) + semantic (pgvector cosine over `text-embedding-3-small` embeddings), each leg mapped to a tri-tier confidence (strict / low / none). On `strict` the DB answer is reworded before it ships — NOT by the LLM's own wrapping turn (the display-tool short-circuit in `node_faq` `break`s before one happens), but by `faq_rephrase.rephrase_faq_answer()`, the same guarded helper the pre-check uses; on `low` the closest FAQ questions AND their answer text are surfaced to the LLM as candidates (`FAQ_SEM_TOP_K`) so it can answer directly in the same round instead of needing a second `faq_lookup` call.

`node_faq`'s own deterministic strict pre-check (`faq_precheck_answer`, stricter than the tier above — see its docstring in `app/utils/faq_tools.py`) skips the LLM entirely on an unambiguous strict hit. Since there's no LLM turn to rephrase it there, `app/agent/faq_rephrase.py::rephrase_faq_answer()` runs one extra tight LLM call to reword the DB text naturally, guarded by a deterministic check that discards the rephrase (falling back to the verbatim DB answer) if any number or URL from the source is missing from the result, or the length looks off — switchable via `FAQ_REPHRASE_ENABLED`.

When `state.user_profile` is non-empty, `_format_state_xml()` appends a `<client_profile>` block (facts + relationship notes) to the per-turn `<state>` system message so the LLM can personalize tone without re-asking known facts — see "Personal consultant" memory below.

#### node_recap (`nodes/recap.py`) — resume a stale calc_flow/qualify_flow

Reached from the router in two cases (see pseudocode above). First entry (no `dialog.recap_pending` yet) shows "Продолжим с того места, где остановились — {description}?" with 3 buttons (i18n `btn_recap_continue`/`btn_recap_restart`/`btn_recap_other`) and sets `dialog.recap_pending = True` — this node finalizes that turn itself (static `recap → END` edge). The next turn (now `recap_pending == True`) interprets the reply and returns a `Command(goto=...)`, same dynamic-routing pattern as the router:
- "Продолжить" → clears the flag, `Command(goto="calc_flow"|"qualify_flow")`, dialog otherwise untouched (routes back into whichever flow was in progress, using `dialog.flow` to decide the target).
- "Начать заново" → `_reset_dialog()` + `Command(goto="faq")`.
- anything else (a different question, free text) → clears the flag only, `Command(goto="faq")` — `dialog.flow`/`category`/`selected_product` are deliberately left as-is so the LLM still has that context.

Known limitation: if the stale step was specifically `lead_step == "offer"` ("want us to call?"), the "Продолжить" button text doesn't match that step's yes/recalculate parsing, so it reads as a decline (dialog resets) rather than re-showing the offer — accepted as a rare timing edge case rather than special-cased.

#### node_qualify_flow (`nodes/qualify_flow.py`) — deterministic qualification questionnaire

Pre-listing questionnaire (decision trees in `app/agent/qualify.py`): before showing products, the user answers a few button questions (e.g. deposit currency, credit purpose); answers filter the DB query. Terminal nodes either render the filtered product list (re-entering the normal `select_product` → calculator chain) or show a dead-end message. Side questions mid-questionnaire are answered via LLM, then the current question is re-asked.

**Dead-end rescue (Phase 4)**: `_dead_end_with_rescue()` — when a dead-end node is reached for `mortgage`/`autoloan`/`education_credit` (`_DEAD_END_RESCUE_CATEGORY` map, `qualify_flow.py`), deterministically (no LLM) pitch up to 2 `microloan` products via `recommend.rank_products(products, profile=None, ...)` alongside the fixed dead-end message — a `product_list` (`kind: "recommend"`) ui_block, same shape as `recommend_product`'s pitch. `microloan`'s own dead end (`dead_consider_others`) has no rescue target (it's already the easier fallback for the other three) — unchanged plain message. Deposit/card trees have no dead-end node at all, so nothing to rescue there.

#### node_calc_flow (`nodes/calc_flow.py`) — deterministic calculator + lead capture

Two sub-flows:

**calc_step** — collects inputs for payment calculation:
- Credit: amount → term → downpayment → generates PDF schedule
- Deposit: amount → term → text calculation
- If user asks a side question mid-calc, answers it via LLM then re-asks the current step
- **DTI check (Phase 4)**: at the credit finalization, if `state.user_profile.facts.income_monthly` is known and `monthly_payment / income > DTI_WARN_RATIO` (env, default `0.45`), a soft warning is appended to the reply (reusing the "🔄 Пересчитать" button already in `lead_keyboard` — no new keyboard option needed) and `dti_ratio` is added to the `calc_result` ui_block (`null` when income is unknown). This is a deterministic node — the LLM persona's own soft DTI rule (system policy, Phase 1) never runs on this path, so this check is the only enforcement for the calculator flow itself.

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
    user_profile: dict | None     # {"facts": dict, "notes": str, "updated_at": str|None} or None — see "Personal consultant" memory below
    ui_blocks: list[dict] | None  # Mini App structured cards for this turn — see docs/MINIAPP.md "UI blocks"; Telegram never reads it
```

The `dialog` dict tracks: `flow`, `category`, `products`, `selected_product`, `calc_step`, `calc_slots`, `lead_step`, `lead_slots`, `qualify_category`, `qualify_node`, `qualify_answers`, `fallback_streak`, `last_lang`, `offices`, `selected_office`, `office_type`, `last_turn_at` (ISO timestamp set by `_finalize_turn()` every turn — drives `node_recap`'s staleness check), `last_recommendation` (last `recommend_product` pitch — informational). Sticky session flags (e.g. `lang_switch_offered`) survive dialog resets — use `_reset_dialog()` instead of bare `_default_dialog()` when resetting mid-session; `_finalize_turn()` also carries them over.

### "Personal consultant" memory (`app/agent/profile.py`, `memory_extract.py`, `recommend.py`)

- **`UserProfile`** (`app/db/models.py`) — one row per user (`user_id` FK → `users.id` UNIQUE), `facts` (JSONB, loose schema — `income_monthly`, `age`, `income_type` ∈ {`payroll`,`official`,`no_official`}, `currency`, `family_status`, `goals`, `preferences`) + `notes` (Text — a standing 2-4 sentence relationship recap, replaced wholesale, not appended).
- **Load**: `app/agent/profile.py::load_user_profile(telegram_user_id)` — called once per turn by `Agent._ainvoke_locked` (agent.py), BEFORE `graph.ainvoke`, and placed on `state_in["user_profile"]`. A DB failure is logged and treated as `None` ("no profile") — it must never break the turn. Note the function takes the **Telegram** user id (same as everywhere else in the agent layer) and resolves the internal `users.id` PK itself.
- **Write**: `app/agent/profile.py::upsert_user_profile(telegram_user_id, facts_delta, notes)` — a single atomic `INSERT ... ON CONFLICT (user_id) DO UPDATE` using Postgres's JSONB `||` operator to merge `facts_delta` in (only the given keys are touched); `notes=None` leaves stored notes untouched, any string (including `""`) replaces them wholesale. `facts_delta={}` + `notes=None` is a no-op (no DB read/write at all).
- **Background extraction**: `app/agent/memory_extract.py::extract_and_store(user_id, user_text, answer, current_profile)` — fired from `Agent._ainvoke_locked` via `asyncio.create_task` AFTER `graph.ainvoke` (NOT awaited), skipped for `human_mode` turns and empty answers, gated by `MEMORY_EXTRACT_ENABLED`. Uses the `"extractor"` LLM role with a single Russian-only internal prompt (never shown to the customer) that returns strict JSON `{"facts": {...}|{}, "notes": "..."|null}`; an empty result is a normal, frequent outcome. `Agent._background_tasks: set[asyncio.Task]` holds a strong reference until each task's done-callback removes it (otherwise the event loop could GC an in-flight task) and logs any unhandled exception.
- **Qualify prefill**: `app/agent/qualify.py` trees mark select nodes with `profile_key` (+ a per-node `profile_answer_text` map) — e.g. `autoloan`/`mortgage`/`microloan`'s `salary`/`salary_card`/`self_employed` nodes all key off `facts.income_type`. `prefill_from_profile()` walks the tree from entry exactly like the existing text-based `prefill()`, converting a known fact into representative text and resolving it through the SAME `match_answer()` — so it never duplicates the acceptance logic. `qualify_flow.py::start_qualify()` runs the profile-based walk first, then continues the text-based `prefill()` from wherever it stopped (the two compose), and prefixes the reply with an acknowledgement ("Исходя из того, что...") when anything was skipped. A node with no `profile_key` is a hard stop by design — only entry-adjacent income-type questions are marked; branch-specific questions (auto brand, mortgage market, microloan channel) are deliberately left for the customer to answer.
- **`recommend_product` tool**: `app/agent/recommend.py::rank_products(products, profile, goal_category)` is a pure, deterministic ranking function (no LLM) over a single category's products — score = rate (ascending) + an age-fit bonus/penalty when `facts.age` falls inside/outside the product's rate-tier age bounds. The tool (`tools.py`) maps the customer's goal text to a category via `intent.py::_detect_product_category` (reused, not duplicated), falling back to `dialog.category`. `nodes/faq.py::_update_dialog_from_tools` re-runs the same ranking (needs `state.user_profile`, which the dialog-update helper doesn't otherwise see) so the exact top-2 pitched products land in `dialog.products` and `select_product` keeps working next turn.
- **Personalized rate line**: `products.py::_format_product_card()` adds a "✨ Для вас: X%" line next to the base "от X%" when `facts.age`/`facts.income_type` resolve a credit product's rate unambiguously — via the EXISTING `rate_rules.select_rate_value(rules, age=..., income_type=...)` (already accepts partial kwargs and excludes any rule needing amount/term data we don't have — no new matching function was needed).

### LLM Configuration (`app/agent/llm.py`)

- Uses `langchain-openai` / `ChatOpenAI`
- **Role-based model factory**: `_get_chat_openai(role=...)` builds/caches a separate client per role (`_build_chat_openai`, `lru_cache(maxsize=4)`, keyed by role). Roles: `"consultant"` (customer-facing — `node_faq`, and the side-question LLM calls in `calc_flow.py`/`qualify_flow.py`, since those reply to the customer directly) and `"extractor"` (structured JSON extraction in `calc_extractor.py`, 3 call sites). Resolution priority per role: `<ROLE>_LLM_MODEL` env (`CONSULTANT_LLM_MODEL` / `EXTRACTOR_LLM_MODEL`) → `OPENAI_MODEL` → `LOCAL_AGENT_INTENT_LLM_MODEL` → role default (`gpt-4o` for consultant, `gpt-4o-mini` for extractor). Existing deployments that only set `OPENAI_MODEL` are unaffected by the new `gpt-4o` default — the legacy var still outranks it. In Qwen mode (`USE_GPT=false`) roles are NOT split — every role gets `_qwen_model_name()`, unchanged from before.
- Supports custom `OPENAI_BASE_URL` for OpenAI-compatible APIs
- Built-in token usage tracking and cost calculation per model
- **Provider switch (`USE_GPT`)**: `true` (default) → OpenAI; `false` → Qwen via Together AI (`QWEN_MODEL`/`QWEN_BASE_URL`/`QWEN_API_KEY` or `TOGETHER_API_KEY`). The switch covers BOTH the main agent LLM and the language detector (`lang_detect.py`) via the shared `provider_connection()` helper. The detector can use a cheaper model with `QWEN_LANG_DETECTOR_MODEL` (Qwen mode) or `LANG_DETECTOR_MODEL` (GPT mode).
- **FAQ embeddings always use OpenAI** (`app/utils/embeddings.py` reads `OPENAI_API_KEY`/`OPENAI_BASE_URL` directly, ignoring `USE_GPT`) — semantic FAQ search stays on OpenAI even when chat runs on Qwen. Keep `OPENAI_API_KEY` set, or disable semantic search with `FAQ_EMBEDDING_ENABLED=false`.
- Cost tracking only knows OpenAI prices (`_MODEL_PRICING`); Qwen turns report `cost=0`.
- **`LLM_MAX_TOKENS`** (default `3000`): output token cap for the main agent LLM. For non-reasoning models this is a cap (they stop when done). Reasoning models (e.g. `openai/gpt-oss-20b`) share this budget across analysis + final channels — the old 512 was too small for a final answer to fit.
- **`LANG_DETECTOR_MAX_TOKENS`** (default `512`): output token cap for the language detector. Same reasoning: 5 tokens was entirely consumed by the analysis channel on reasoning models.
- **Harmony/reasoning channel stripping**: `extract_text_content()` in `llm.py` automatically strips `<|channel|>...<|message|>` markers leaked by Together AI reasoning models. If a `final` channel is present, only its text is returned; if only an `analysis` channel leaked (answer cut off), `""` is returned so `node_faq` uses its fallback path. Plain model output is returned unchanged. This makes any OpenAI-compatible model work — including reasoning models — though they waste tokens; prefer a non-reasoning instruct model.

### Hybrid Bot/Operator Mode

- `ChatSession.human_mode = True` → user messages are saved to DB and forwarded to the chat-middleware (or routed through the LangGraph `interrupt()` if no middleware chat is active); they are NOT processed by the LLM
- Handoff is triggered by the inline «Живой оператор» button (callbacks `human:<sid>` / `bot:<sid>`) or by the `request_operator` tool (LLM decides, e.g. identity-required operations)
- Operators work in the Asaka chat-middleware; their replies come back over Socket.IO and are delivered to Telegram + resumed into the graph
- The `ChatMiddlewareClient` instance is registered in `app/services/middleware_registry.py` during app startup — get it via `get_middleware_client()`, never import the FastAPI app from services/handlers
- Background inactivity watcher (60s interval) auto-returns stale human-mode sessions to bot after `HUMAN_MODE_OPERATOR_TIMEOUT_MINUTES`
- **Operator handoff summary (Phase 4, `app/agent/handoff.py`)**: right after `ChatMiddlewareClient.start_chat()` succeeds (both `commands.py::enable_human_mode` and `miniapp/routes/chat.py::toggle_operator`), `send_operator_handoff_summary()` builds a short Russian LLM briefing (role `"extractor"`, category/calc_slots/qualify_answers/relevant profile facts/last turns/`request_operator` reason — read via the new read-only `Agent.get_handoff_context(session_id)` → `AgentClient.get_handoff_context`) and sends it as the first `send-message` (`"[Сводка для оператора]\n..."`), persisted with `role="system"` (`ChatService.save_system_note` — never surfaced to the client, both `/chat/history` and `/sessions/{id}` filter to `roles=("user","agent","operator")`). Never blocks or fails the handoff itself — every step is caught and logged.
- **Asaka chat-middleware integration** (Socket.IO + JWT) — full protocol details, sequence diagrams, env vars and run checklist in [docs/CHAT_MIDDLEWARE_INTEGRATION.md](docs/CHAT_MIDDLEWARE_INTEGRATION.md)

### Database Models (`app/db/models.py`)

Core: `User`, `ChatSession`, `Message`, `Lead`, `UserProfile` (facts + relationship notes — see "Personal consultant" memory above)
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
- `LLM_MAX_TOKENS` — output token cap for the main agent LLM (default `3000`); reasoning models need room for analysis + final channels
- `CONSULTANT_LLM_MODEL` — model for the customer-facing consultant role (default: falls back to `OPENAI_MODEL`/`LOCAL_AGENT_INTENT_LLM_MODEL`, else `gpt-4o`)
- `EXTRACTOR_LLM_MODEL` — model for the calculator's structured-extraction role (default: falls back to `OPENAI_MODEL`/`LOCAL_AGENT_INTENT_LLM_MODEL`, else `gpt-4o-mini`)
- `AGENT_TIMEOUT_SECONDS` — per-turn timeout for agent invocation (seconds, default `25`)
- `MAX_DIALOG_TOKENS` — approximate token budget for dialog history sent to the LLM (default `16000`)
- `LANG_DETECTOR_MODEL` — model used by the dedicated language detector (default: `gpt-4o-mini`)
- `LANG_DETECTOR_TIMEOUT` — per-request timeout for the language detector (seconds, default `10`)
- `LANG_DETECTOR_MAX_TOKENS` — output token cap for the language detector (default `512`); reasoning models consume the old 5-token budget entirely in the analysis channel
- `DEFAULT_CUSTOM_LOAN_RATE_PCT` — fallback rate for calculator when product rate is unavailable (default `20.0`)
- `DTI_WARN_RATIO` — debt-to-income warning threshold (Phase 4): a monthly payment above this share of `state.user_profile.facts.income_monthly` triggers a soft warning in `node_calc_flow`'s deterministic credit finalization, and is the verdict threshold in the `affordability_check` tool (default `0.45`)
- `MEMORY_EXTRACT_ENABLED` — background "personal consultant" memory extractor after each turn (default `true`)
- `RECAP_GAP_MINUTES` — minutes of inactivity within an in-progress calc_flow/qualify_flow before `node_recap` offers to resume instead of silently treating the next message as an answer (default `30`)

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
- `FAQ_REPHRASE_ENABLED` — rewords a verified FAQ DB answer into the LLM's own words before it ships, preserving every fact/number/rate/term/link (default `true`); covers both `node_faq`'s deterministic strict pre-check (via `app/agent/faq_rephrase.py`, one extra guarded LLM call since there's no LLM turn there otherwise) and the `faq_lookup` tool's system-policy instruction to rephrase rather than recite

Telegram Mini App:
- `MINIAPP_ENABLED` (default `true`) — mounts `/api/miniapp` and serves the SPA at `/app`
- `MINIAPP_URL` — public **https** URL of the Mini App; sets the bot menu button and powers `/app`
- `MINIAPP_DEV_MODE` (default `false`) — accepts unsigned `initData` as `MINIAPP_DEV_USER_ID` and enables localhost CORS. Browser-testing only; it disables authentication entirely
- `MINIAPP_DEV_USER_ID` (default `111111111`)
- `MINIAPP_INIT_DATA_TTL_SECONDS` (default `86400`) — max age of a signed launch payload
- `MINIAPP_DIST_DIR` — override the built SPA directory (default `frontend/dist`)
- `MINIAPP_STREAMING_ENABLED` (default `true`) — live `node_faq` token streaming to the Mini App over `/api/miniapp/ws` (`assistant_token`/`assistant_done` events). Telegram is unaffected either way — see docs/MINIAPP.md "UI blocks"

`SESSION_INACTIVITY_TIMEOUT_MINUTES` doubles as the Mini App session TTL: the watcher closes a stale session with `closed_reason="timeout"`, which the history screen renders as an expired (read-only) conversation.

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
5. If the tool's output should render as a Mini App card, add `response_format="content_and_artifact"` and return `(text, {"type": ..., "data": ...} | None)` — see docs/MINIAPP.md "UI blocks" for the block-type contract; otherwise leave it returning plain text (`node_faq` accumulates whatever artifacts show up automatically, no other change needed)

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
