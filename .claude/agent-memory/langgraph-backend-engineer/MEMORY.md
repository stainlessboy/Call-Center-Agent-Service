# LangGraph Backend Engineer — Project Memory

## Project Identity
- Telegram banking chatbot: aiogram + FastAPI + LangGraph + SQLAlchemy async
- Entry: `main.py` → uvicorn on `APP_HOST:APP_PORT` (default 0.0.0.0:8001)
- Core agent: `app/services/agent.py` (~3500 lines post-refactor)
- Checkpointing: configurable via `LANGGRAPH_CHECKPOINT_BACKEND` (memory/sqlite/postgres/auto)
- Tests: `tests/test_agent.py`

## Graph Architecture (current, post-refactor)
Multi-node graph replacing old single-node:
```
classify → followup / flow / new_turn / human_mode → END
```
- `node_classify_turn`: routes via `state["_route"]` ("followup"|"flow"|"new"|"human")
- `node_followup_turn`: LLM contextual reply for short conversational follow-ups
- `node_flow_turn`: active product flow step (mortgage, auto_loan, etc.)
- `node_new_turn`: FAQ lookup or start new product flow
- `node_human_mode_turn`: uses `langgraph.types.interrupt()` to pause for operator
- `node_process_turn`: kept as dead code (no longer in graph)

## BotState Fields
```python
class BotState(TypedDict, total=False):
    messages: List[Any]       # LangChain message history
    last_user_text: str
    answer: str
    dialog: Dict[str, Any]    # {flow, step, slots}
    human_mode: bool          # operator takeover signal
    _route: str               # internal routing (set by classify node)
```

## Tools Package (`app/tools/`)
Extracted from agent.py during 2026-02 refactor:
- `app/tools/data_loaders.py` — `_fmt_pct`, `_normalize_language_code`, all `_load_*` async+sync
- `app/tools/credit_tools.py` — `_credit_offers_by_section`, `_credit_program_names`, `_all_credit_categories_overview`, `_fmt_rate/term/downpayment_range`, `_offer_matches_*`, `_select_exact/near_*`, `_format_exact/near_credit_offers_reply`
- `app/tools/deposit_tools.py` — `_select_deposit_options`, `_pick_deposit` (mutual recursion OK; define select first)
- `app/tools/card_tools.py` — `_select_debit/fx_card_options`, `_pick_debit/fx_card` (same pattern)
- `app/tools/faq_tools.py` — `FAQ_FALLBACK_REPLY`, `_normalize_text`, `_token_stem`, `_token_set`, `_faq_similarity`, `_faq_lookup`

## Agent Class / File Names
- File: `app/services/agent.py` (was `local_agent.py`)
- Class: `Agent` (was `LocalAgent`)
- Wrapper: `app/services/agent_client.py` → `AgentClient._agent` (was `._local_agent`)

## Agent Key Patterns
- `setup(backend, url)` must be called at startup (from `AgentClient.setup()` in lifespan)
- Uses `ainvoke` (async) via async checkpointer
- `InMemoryStore` for cross-session user language preferences
- `_create_async_checkpointer(backend, url)` — graceful fallback: postgres → sqlite → MemorySaver
- `resume_human_mode(session_id, operator_reply)` — resumes interrupted graph via `Command(resume=...)`
- `aclose()` — calls `__aexit__` on non-MemorySaver checkpointers

## Checkpointer Config
- `LANGGRAPH_CHECKPOINT_BACKEND=auto|sqlite|postgres|memory`
- `LANGGRAPH_CHECKPOINT_URL` — SQLite path or Postgres DSN
- Default SQLite path: `.langgraph_checkpoints.sqlite3`
- Postgres: uses `AsyncPostgresSaver` from `langgraph.checkpoint.postgres.aio`
- SQLite: uses `AsyncSqliteSaver` from `langgraph.checkpoint.sqlite.aio`

## Human-Mode Flow
1. User message → `ChatService.handle_user_message` → `human_mode=True` → calls agent with `human_mode=True`
2. Graph routes to `node_human_mode_turn` → calls `langgraph_interrupt()`
3. Operator replies via `/op` prefix or `/operator/send` REST API
4. `send_operator_message` in ChatService → saves DB message → calls `agent_client.resume_human_mode()`
5. `resume_human_mode` → `graph.ainvoke(Command(resume=operator_reply), config)`

## Adding New Features (established pattern)
1. ORM model → `app/db/models.py`
2. Alembic migration: `alembic revision -m "..." --autogenerate`
3. Selector/tool function in `app/tools/` (appropriate module) or `agent.py`
4. Register intent in LLM classification prompt + `node_route`/`_classify_new_intent_rules`
5. Add data JSON under `app/data/ai_chat_info/`
6. Create seed script in `scripts/`

## Key File Locations
- Agent: `app/services/agent.py`
- Agent tools: `app/tools/` (data_loaders, credit_tools, deposit_tools, card_tools, faq_tools)
- Agent wrapper: `app/services/agent_client.py`
- Chat logic: `app/services/chat_service.py`
- FastAPI + lifespan: `app/api/fastapi_app.py`
- Telegram handlers: `app/bot/handlers/commands.py`
- Settings: `app/config.py` (dataclass + `@lru_cache get_settings()`)
- Tests: `tests/test_agent.py`

## Async Pitfalls Encountered
- `asyncio.to_thread` for sync LangGraph invoke is safe but loses event loop benefits; prefer `ainvoke`
- SQLAlchemy session scoping: always use `async with session_factory() as session:` per-operation
- MemorySaver does NOT need `__aenter__`/`__aexit__`; AsyncSqliteSaver and AsyncPostgresSaver do
- Mutual recursion between `_pick_deposit`/`_select_deposit_options` (and card equivalents) is fine because Python resolves names at call time, not definition time

## Message History Trim
- `MAX_DIALOG_MESSAGES` env var (default 50) controls max messages kept
- Formula: `[system_msg] + last N messages` — system message always preserved at index 0
- Applied in both `node_process_turn` (legacy) and `_finalize_turn` (new nodes)
