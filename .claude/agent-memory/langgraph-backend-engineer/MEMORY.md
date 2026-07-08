# LangGraph Backend Engineer — Project Memory

NOTE (2026-07-02): This file was rewritten after finding the previous version
stale — it described a pre-refactor single-file `app/services/agent.py` with
a 14-node router fanout (`node_faq_llm`, `app/tools/*`, etc.). The project has
since moved to a modular `app/agent/` package with a 5-node graph. CLAUDE.md
is the authoritative architecture doc — verify against it (and the actual
files) before trusting anything below if it's been a while.

## Project Identity
- Telegram banking chatbot: aiogram + FastAPI + LangGraph + SQLAlchemy async
- Entry: `main.py` → uvicorn on `APP_HOST:APP_PORT` (default 0.0.0.0:8001)
- Core agent package: `app/agent/` (modular — NOT a single monolithic file)
- Checkpointing: `LANGGRAPH_CHECKPOINT_BACKEND` (memory/postgres/auto)
- Tests: `tests/` — pytest + pytest-asyncio, `@pytest.mark.asyncio` explicit
  markers (no `asyncio_mode=auto` configured)

## Graph Architecture (current — see CLAUDE.md for full detail)
```
START → router → faq | calc_flow | qualify_flow | human_mode → END
```
5 nodes. `Command(goto=...)` routing, no explicit conditional edges.
- `app/agent/nodes/router.py` — node_router, deterministic dispatch
- `app/agent/nodes/faq.py` — node_faq, LLM + 11 tools via `bind_tools`, manual
  round loop (max 3 rounds) using module-level `_FAQ_TOOL_NODE = ToolNode(...)`
- `app/agent/nodes/calc_flow.py` — deterministic calculator + lead capture
- `app/agent/nodes/qualify_flow.py` — pre-listing questionnaire
- `app/agent/nodes/human_mode.py` — `interrupt()` for operator handoff
- `app/agent/nodes/helpers.py` — `_finalize_turn()` shared by all nodes

## Key Files
- `app/agent/agent.py` — `Agent` class, `_ainvoke()` is the per-turn entry point
- `app/agent/graph.py` — StateGraph wiring
- `app/agent/state.py` — `BotState` TypedDict, `_default_dialog()`/`_reset_dialog()`
- `app/agent/llm.py` — ChatOpenAI factory, token usage/cost, harmony-channel stripping
- `app/agent/tools.py` — 11 `@lc_tool` functions + `_FAQ_TOOLS` list
- `app/utils/faq_tools.py` — hybrid FAQ search (`faq_search`, lexical+semantic tri-tier)
- `app/agent/pii_masker.py` — regex PII masking before text reaches OpenAI

## Established Patterns
- [LLM factory: lru_cache must never cache a None/failure](llm_factory_pattern.md) — split build (raises) from getter (catches+logs). Applied to BOTH `app/agent/llm.py::_get_chat_openai` (2026-07-02) and `app/agent/lang_detect.py::_get_detector_llm` (2026-07-07 — was the last un-fixed instance, now fixed).
- [Per-turn memoization via contextvars](contextvar_turn_cache.md) — pattern for caching within one agent turn/asyncio task without a real cache layer
- [FLOW_CALC/FLOW_QUALIFY escape-hatch + fallback_streak gap](flow_escape_hatch_gap.md) — found 2026-07-07 audit, **fixed same day**: `_is_back_trigger` now checked at the top of `node_calc_flow`/`node_qualify_flow` (resets via `_reset_dialog`, shows product list or main menu), and the reprompt/re-ask `_finalize_turn` calls in both files now pass `is_fallback=True` (qualify_flow's re-ask dialog also carries `fallback_streak` forward instead of implicitly zeroing it). Regression tests in `tests/test_robustness_fixes.py`.
- [Per-session concurrency + checkpointer-read-failure risks](agent_turn_concurrency_risks.md) — found 2026-07-07 audit, **fixed same day**: `Agent` now holds a `_session_locks: dict[str, asyncio.Lock]` serializing `_ainvoke`/`resume_human_mode` per `session_id` (single-process only — see class docstring), and `_aload_existing_state` retries a failed `aget_state` (backoffs `(0.2, 0.5)`) then **raises** instead of silently returning `{}` (a brand-new-session empty snapshot is still not an error). Regression tests in `tests/test_robustness_fixes.py`.
- `scripts/*.py` (e.g. `diag_model.py`) have historically embedded hardcoded bearer tokens for external GPU endpoints inline as dict literals — **fixed 2026-07-07**: tokens now come from `--token` / `DIAG_TOKEN_<PRESET>` / `DIAG_MODEL_TOKEN` env, `PRESETS` only holds model+url. Still always grep new/untracked scripts for `token`/`api_key`/`Bearer` before they get committed — this pattern can recur.
- `custom_loan_calculator` (tools.py) now clamps free-form input: `term_months` ≤ 600, `amount` ≤ 10bn UZS (module constants `_MAX_CUSTOM_LOAN_TERM_MONTHS`/`_MAX_CUSTOM_LOAN_AMOUNT`), returning an i18n range message instead of risking `OverflowError` in the annuity formula for pathological input. Fixed 2026-07-07.
- Rate-related config (`DEFAULT_CUSTOM_LOAN_RATE_PCT`) now lives on `app.config.Settings.default_custom_loan_rate_pct`, read live via `get_settings()` in both `app/agent/tools.py::custom_loan_calculator` and `app/agent/nodes/calc_flow.py::_lookup_credit_rate` — NOT a module-level `os.getenv(...)` constant anymore (that pattern required a process restart, or `importlib.reload` in tests, to pick up env changes). Fixed 2026-07-07; if you add new tunable numeric constants to the agent, prefer `Settings` + `get_settings()` over a fresh module-level `os.getenv` read.
- Model backend is a live moving target: `.env`'s `OPENAI_MODEL`/`USE_GPT`/`QWEN_MODEL`/`QWEN_BASE_URL` and `MODEL_BENCHMARK_REPORT.md`'s recommendation can disagree (e.g. benchmark recommends a model that `.env` doesn't point at yet) — always check current `.env` state directly rather than assuming the latest benchmark report reflects production.
- Every `_get_chat_openai()` call site MUST guard with `if llm is None` / `if not llm` before calling `.bind_tools()` / `.ainvoke()` — a bare call crashes on `None.bind_tools(...)`. Call sites as of 2026-07: `app/agent/nodes/faq.py`, `app/agent/nodes/calc_flow.py`, `app/agent/nodes/qualify_flow.py`, `app/agent/calc_extractor.py` (x3).
- Multi-tool LLM binds (`llm.bind_tools(_FAQ_TOOLS)`) should pass
  `parallel_tool_calls=False` wrapped in `try/except TypeError` (some
  OpenAI-compatible backends, e.g. Gemma via vLLM, reject the kwarg at
  bind-time or just ignore it and emit parallel calls anyway — the
  `node_faq` tool loop assumes one tool call per round).
- Tests that stub the LLM in `node_faq` use a minimal fake class with
  `bind_tools(self, tools)` (see `tests/test_pii_masker.py::TestFaqNodePiiBoundary`)
  — this deliberately lacks a `parallel_tool_calls` kwarg, so it's the
  regression test shape for the TypeError-fallback path.

## Test Conventions
- `tests/test_faq_embeddings.py` pattern for `faq_search`: `monkeypatch.setenv(...)` +
  `get_settings.cache_clear()`, then `patch("app.utils.faq_tools._lexical_lookup", ...)` /
  `_semantic_lookup` with `AsyncMock`. To avoid hitting Postgres, mock
  `app.utils.embeddings.embed_texts` to return `[None]` — `_semantic_lookup`
  short-circuits on a None vector before touching the DB.
- `tests/test_agent.py` has ~20 sites calling `_llm._get_chat_openai.cache_clear()`
  directly — any refactor of the LLM factory's caching must preserve that
  exact attribute name/callable on `_get_chat_openai`.
- Run tests via `source .venv/bin/activate && python3 -m pytest ...` — bare
  `python3 -m pytest` fails (`No module named pytest`) since pytest is only
  installed in the project venv, not system Python.

## Workflow Preferences (from CLAUDE.md, confirmed stable)
- Plan first for non-trivial tasks; commit messages suggested in Russian, never auto-committed
- New tools: add `@lc_tool` in `app/agent/tools.py` → register in `_FAQ_TOOLS` →
  if it changes dialog state, add a handler in `nodes/faq.py::_update_dialog_from_tools()`
- New product types: ORM model → Alembic migration → `CREDIT_SECTION_MAP` in
  `constants.py` → `_get_products_by_category()` in `products.py` → i18n → seed service
