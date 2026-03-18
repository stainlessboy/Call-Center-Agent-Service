# LangGraph Backend Engineer — Project Memory

## Project Identity
- Telegram banking chatbot: aiogram + FastAPI + LangGraph + SQLAlchemy async
- Entry: `main.py` → uvicorn on `APP_HOST:APP_PORT` (default 0.0.0.0:8001)
- Core agent: `app/services/agent.py`
- Checkpointing: configurable via `LANGGRAPH_CHECKPOINT_BACKEND` (memory/sqlite/postgres/auto)
- Tests: `tests/test_agent.py`

## Graph Architecture (current, post-refactor 2026-03)
Multi-node fanout: router dispatches to 14 specialized nodes via `Command(goto=...)`.
```
START → router →
  greeting / thanks / back_to_list / start_calc / select_product /
  product_menu / credit_menu / branch_info / currency_info /
  comparison / calc_step / lead_step / faq / human_mode → END
```
- `node_router`: async, two-level: deterministic L1 then LLM L2
- `faq` node: `node_faq_llm` — ChatOpenAI.bind_tools + manual ToolNode loop
- `calc_step`: calc collection + PDF generation (asyncio.to_thread for PDF)
- `lead_step`: offer → name → phone → DB save
- `human_mode`: uses `langgraph.types.interrupt()`

## BotState Fields
```python
class BotState(TypedDict):
    messages: List[Any]; last_user_text: str; answer: str
    human_mode: bool; keyboard_options: Optional[List[str]]
    dialog: dict  # flow/category/products/selected_product/calc_step/calc_slots/lead_step/lead_slots
    _route: str; session_id: Optional[str]; user_id: Optional[int]
```

## Router Two-Level Pattern
L1 deterministic: human_mode → lead_step → calc_flow → back_to_list → start_calc → select_product
L2 LLM: `_llm_classify_intent()` via `asyncio.to_thread`, OpenAI function_call with `_INTENT_SCHEMA`
Heuristic fallback when `LOCAL_AGENT_INTENT_LLM_ENABLED=0` or no OPENAI_API_KEY.
Category routing: `Command(goto="product_menu", update={"dialog": {**dialog, "_pending_category": cat}})`

## Key Implementation Notes
- `_finalize_turn` returns `dict` (not mutated BotState) — correct LangGraph pattern
- `_get_products_by_category` is now **async** (removed old `_load_*_sync` wrappers)
- `_save_lead_async` uses `get_session()` not `AsyncSessionLocal`
- Sync OpenAI calls in async context: `asyncio.to_thread(lambda: client.create(...))`
- `generate_amortization_pdf` is sync: wrap with `asyncio.to_thread` in async nodes

## faq_llm ToolNode Loop Pattern
```python
llm_with_tools = llm.bind_tools([faq_lookup, get_products])
for _ in range(3):
    ai_msg = await llm_with_tools.ainvoke(loop_msgs)
    loop_msgs.append(ai_msg)
    if not ai_msg.tool_calls: break
    tool_results = await ToolNode([...]).ainvoke({"messages": loop_msgs})
    loop_msgs.extend(tool_results["messages"])  # ToolNode returns {"messages": [...]}
```

## Test Pattern
- `os.environ.setdefault("LOCAL_AGENT_INTENT_LLM_ENABLED", "0")` at top of test file
- Forces heuristic routing — deterministic without mocking OpenAI
- `build_graph()` has 15 nodes: router + 14 destinations

## Agent Class API (unchanged, public contract)
- `setup(backend, url)` — call at startup
- `send_message(session_id, user_id, text, language, human_mode)` → `AgentTurnResult`
- `resume_human_mode(session_id, operator_reply)` → str
- `sync_history(session_id, events)` → None
- `aclose()` — cleanup checkpointer

## Key File Locations
- Agent: `app/services/agent.py`
- Data loaders (async only): `app/tools/data_loaders.py`
- FAQ tools: `app/tools/faq_tools.py` (async `_faq_lookup`)
- Agent wrapper: `app/services/agent_client.py`
- Chat logic: `app/services/chat_service.py`
- FastAPI + lifespan: `app/api/fastapi_app.py`
- Telegram handlers: `app/bot/handlers/commands.py`
- Settings: `app/config.py`
- Tests: `tests/test_agent.py`

## Checkpointer Config
- Backend: `LANGGRAPH_CHECKPOINT_BACKEND=auto|sqlite|postgres|memory`
- URL: `LANGGRAPH_CHECKPOINT_URL` — SQLite path or Postgres DSN
- Fallback chain: postgres → sqlite → MemorySaver
- Only AsyncSqliteSaver and AsyncPostgresSaver need `__aenter__`/`__aexit__`

## Adding New Nodes (established pattern)
1. Write `async def node_xyz(state: BotState) -> dict:` returning `_finalize_turn(...)`
2. Register in `build_graph()`: `graph.add_node("xyz", node_xyz)` + `graph.add_edge("xyz", END)`
3. Add routing in `node_router` L1 (deterministic) or extend `_INTENT_SCHEMA` enum + L2 handler
4. For new product types: ORM → migration → `_get_products_by_category` → seed script
