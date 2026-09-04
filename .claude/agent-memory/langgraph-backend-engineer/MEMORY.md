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
START → router → faq | calc_flow | qualify_flow | human_mode | recap → END
```
6 nodes (Phase 2 added `recap`). `Command(goto=...)` routing, no explicit conditional edges.
- `app/agent/nodes/router.py` — node_router, deterministic dispatch
- `app/agent/nodes/faq.py` — node_faq, LLM + 16 tools via `bind_tools`, manual
  round loop (max 5 rounds as of 2026-08-06, was 3) using module-level
  `_FAQ_TOOL_NODE = ToolNode(...)`. Phase 3 (2026-08-06): accumulates tool
  `ToolMessage.artifact` into `state.ui_blocks` every round; `_run_llm_round()`
  streams via `.astream()` when a Mini App turn armed `app/agent/streaming.py`'s
  `on_token` contextvar, else plain `.ainvoke()` (Telegram — unchanged).
  Phase 4 (2026-08-10): `dialog["clarify_last_turn"]` anti-loop guard in the
  round loop (see [[project-consultant-redesign]] Phase 4 for mechanics).
- `app/agent/nodes/calc_flow.py` — deterministic calculator + lead capture
- `app/agent/nodes/qualify_flow.py` — pre-listing questionnaire
- `app/agent/nodes/human_mode.py` — `interrupt()` for operator handoff
- `app/agent/nodes/recap.py` — resume-a-stale-flow offer (Phase 2)
- `app/agent/nodes/helpers.py` — `_finalize_turn()` shared by all nodes

## Key Files
- `app/agent/agent.py` — `Agent` class, `_ainvoke()` is the per-turn entry point
- `app/agent/graph.py` — StateGraph wiring
- `app/agent/state.py` — `BotState` TypedDict (incl. `ui_blocks`, Phase 3), `_default_dialog()`/`_reset_dialog()`
- `app/agent/llm.py` — ChatOpenAI factory, token usage/cost, harmony-channel stripping
- `app/agent/tools.py` — 16 `@lc_tool` functions + `_FAQ_TOOLS` list (10 use `response_format="content_and_artifact"` as of Phase 4)
- `app/agent/rate_rules.py` — rate-rule matching engine + `resolve_effective_rate()`/`income_type_from_dialog()` (Phase 4 — the shared 3-tier rate fallback chain, was duplicated in nodes/calc_flow.py before)
- `app/agent/ui_blocks.py` — `calc_result` block builders shared by `tools.py` and `nodes/calc_flow.py` (Phase 3)
- `app/agent/streaming.py` — Mini App token-streaming contextvar (Phase 3)
- `app/agent/handoff.py` — operator handoff LLM summary (Phase 4)
- `app/agent/faq_rephrase.py` — rewords a verbatim FAQ DB answer for node_faq's deterministic pre-check (2026-08-11)
- `app/utils/faq_tools.py` — hybrid FAQ search (`faq_search`, lexical+semantic tri-tier)
- `app/agent/pii_masker.py` — regex PII masking before text reaches OpenAI

## Established Patterns
- [FAQ stopword-aware lexical scoring + DB-answer rephrasing (2026-08-11)](faq_stopword_rephrase_pattern.md) — `token_set_content()` (text_utils.py) strips question-frame filler words from the FAQ token-F1 leg only; `FAQ_LOW_CONFIDENCE` candidates now carry answer text; `app/agent/faq_rephrase.py` rewords a verbatim DB answer with a number/URL-preserving guard, wired into BOTH the deterministic pre-check AND `node_faq`'s display-tool short-circuit (the second wiring was a same-day follow-up fix — a system-policy instruction has zero effect on any path that assigns `answer` and `break`s without an LLM turn in between; grep for all such paths, not just the obvious one). Read before touching `_faq_similarity`, `faq_lookup`'s low-confidence payload, or anything that returns a raw FAQ answer to the user.
- ["Personal consultant" redesign, Phase 1-4 (2026-08-10)](project_consultant_redesign.md) — Phase 1: role-based LLM factory, persona prompt, MAX_DIALOG_TOKENS/max_rounds. Phase 2 ("Память"): UserProfile model+migration, profile.py load/merge, memory_extract.py background extractor, recap node (6th graph node), recommend_product tool (12th tool), qualify.py profile-prefill (per-node text-map gotcha!), personalized rate line. Phase 3 ("Mini App UX"): `ui_blocks` structured cards (7 tools → `content_and_artifact`, `app/agent/ui_blocks.py`, calc_flow/qualify_flow direct blocks), `Message.ui_blocks` JSONB persistence, live `node_faq` token streaming (`app/agent/streaming.py`). Phase 4 ("Экспертиза"): compare_products/what_if_scenario/affordability_check tools (16th-13th), clarify re-enabled + programmatic anti-loop guard, resolve_effective_rate DRY refactor, deterministic DTI check (calc_flow), qualify dead-end rescue, operator handoff summary (`app/agent/handoff.py`). Read before starting Phase 5+.
- [ui_blocks / content_and_artifact / streaming mechanics (Phase 3)](ui_blocks_streaming_pattern.md) — the tool-return-tuple contract, the ToolNode-needs-real-graph-context testing gotcha, the QUALIFY_CATEGORIES leak trap, and the streaming buffering design. Read before touching any `_FAQ_TOOLS` tool's return shape or `node_faq`'s round loop.
- [FAQ pre-LLM bypass needs stricter multi-leg bar than LLM-in-the-loop tools](faq_precheck_strictness.md) — fixed 2026-07-23: `node_faq`'s deterministic strict pre-check was using `faq_search`'s combined (max-of-legs) tier and hijacking product requests (mortgage→microloan); new `faq_precheck_answer()` requires BOTH lexical+semantic legs strict independently. `faq_lookup` tool's `_faq_lookup()`/combined tier is unchanged (LLM stays in the loop there).
- [LLM factory: lru_cache must never cache a None/failure](llm_factory_pattern.md) — split build (raises) from getter (catches+logs). Applied to BOTH `app/agent/llm.py::_get_chat_openai` (2026-07-02) and `app/agent/lang_detect.py::_get_detector_llm` (2026-07-07 — was the last un-fixed instance, now fixed).
- [Per-turn memoization via contextvars](contextvar_turn_cache.md) — pattern for caching within one agent turn/asyncio task without a real cache layer
- [FLOW_CALC/FLOW_QUALIFY escape-hatch + fallback_streak gap](flow_escape_hatch_gap.md) — found 2026-07-07 audit, **fixed same day**: `_is_back_trigger` now checked at the top of `node_calc_flow`/`node_qualify_flow` (resets via `_reset_dialog`, shows product list or main menu), and the reprompt/re-ask `_finalize_turn` calls in both files now pass `is_fallback=True` (qualify_flow's re-ask dialog also carries `fallback_streak` forward instead of implicitly zeroing it). Regression tests in `tests/test_robustness_fixes.py`.
- [Per-session concurrency + checkpointer-read-failure risks](agent_turn_concurrency_risks.md) — found 2026-07-07 audit, **fixed same day**: `Agent` now holds a `_session_locks: dict[str, asyncio.Lock]` serializing `_ainvoke`/`resume_human_mode` per `session_id` (single-process only — see class docstring), and `_aload_existing_state` retries a failed `aget_state` (backoffs `(0.2, 0.5)`) then **raises** instead of silently returning `{}` (a brand-new-session empty snapshot is still not an error). Regression tests in `tests/test_robustness_fixes.py`.
- `scripts/*.py` (e.g. `diag_model.py`) have historically embedded hardcoded bearer tokens for external GPU endpoints inline as dict literals — **fixed 2026-07-07**: tokens now come from `--token` / `DIAG_TOKEN_<PRESET>` / `DIAG_MODEL_TOKEN` env, `PRESETS` only holds model+url. Still always grep new/untracked scripts for `token`/`api_key`/`Bearer` before they get committed — this pattern can recur.
- `custom_loan_calculator` (tools.py) now clamps free-form input: `term_months` ≤ 600, `amount` ≤ 10bn UZS (module constants `_MAX_CUSTOM_LOAN_TERM_MONTHS`/`_MAX_CUSTOM_LOAN_AMOUNT`), returning an i18n range message instead of risking `OverflowError` in the annuity formula for pathological input. Fixed 2026-07-07.
- Rate-related config (`DEFAULT_CUSTOM_LOAN_RATE_PCT`) now lives on `app.config.Settings.default_custom_loan_rate_pct`, read live via `get_settings()` in both `app/agent/tools.py::custom_loan_calculator` and `app/agent/nodes/calc_flow.py::_lookup_credit_rate` — NOT a module-level `os.getenv(...)` constant anymore (that pattern required a process restart, or `importlib.reload` in tests, to pick up env changes). Fixed 2026-07-07; if you add new tunable numeric constants to the agent, prefer `Settings` + `get_settings()` over a fresh module-level `os.getenv` read.
- Model backend is a live moving target: `.env`'s `OPENAI_MODEL`/`USE_GPT`/`QWEN_MODEL`/`QWEN_BASE_URL` and `MODEL_BENCHMARK_REPORT.md`'s recommendation can disagree (e.g. benchmark recommends a model that `.env` doesn't point at yet) — always check current `.env` state directly rather than assuming the latest benchmark report reflects production.
- Every `_get_chat_openai(role=...)` call site MUST guard with `if llm is None` / `if not llm` before calling `.bind_tools()` / `.ainvoke()` — a bare call crashes on `None.bind_tools(...)`. Call sites as of 2026-08-06: `app/agent/nodes/faq.py` (role="consultant"), `app/agent/nodes/calc_flow.py` + `qualify_flow.py` side-questions (role="consultant"), `app/agent/calc_extractor.py` (x3, role="extractor"). See [[llm-factory-pattern]] for the role param mechanics.
- Multi-tool LLM binds (`llm.bind_tools(_FAQ_TOOLS)`) should pass
  `parallel_tool_calls=False` wrapped in `try/except TypeError` (some
  OpenAI-compatible backends, e.g. Gemma via vLLM, reject the kwarg at
  bind-time or just ignore it and emit parallel calls anyway — the
  `node_faq` tool loop assumes one tool call per round).
- Tests that stub the LLM in `node_faq` use a minimal fake class with
  `bind_tools(self, tools)` (see `tests/test_pii_masker.py::TestFaqNodePiiBoundary`)
  — this deliberately lacks a `parallel_tool_calls` kwarg, so it's the
  regression test shape for the TypeError-fallback path.
- When hand-writing an Alembic migration's revision id (not using
  `alembic revision --autogenerate`), grep `app/db/alembic/versions/` for the
  chosen hex string FIRST — a "nice-looking" manually-picked id can collide
  with an existing migration elsewhere in the repo (hit this 2026-08-06:
  `a1b2c3d4e5f6` was already used by `lead_idempotency_and_source.py`,
  `alembic upgrade head` failed with "Multiple head revisions present" until
  renamed). `python3 -c "import uuid; print(uuid.uuid4().hex[:12])"` avoids it.

## Test Conventions
- `tests/test_faq_embeddings.py` pattern for `faq_search`: `monkeypatch.setenv(...)` +
  `get_settings.cache_clear()`, then `patch("app.utils.faq_tools._lexical_lookup", ...)` /
  `_semantic_lookup` with `AsyncMock`. To avoid hitting Postgres, mock
  `app.utils.embeddings.embed_texts` to return `[None]` — `_semantic_lookup`
  short-circuits on a None vector before touching the DB.
- `tests/test_agent.py` has ~20 sites calling `_llm._get_chat_openai.cache_clear()`
  directly — any refactor of the LLM factory's caching must preserve that
  exact attribute name/callable on `_get_chat_openai`.
- Any test stubbing `_get_chat_openai` (directly or via a module alias) must
  accept a `role` kwarg, e.g. `lambda role=None: None` — `_get_chat_openai`
  now takes `role: str = "consultant"` (see [[llm-factory-pattern]]); a bare
  `lambda: None` raises TypeError the moment the real call site passes
  `role=...`. Bit 4 tests in the 2026-08-06 redesign, all fixed by widening
  the stub signature.
- Run tests via `source .venv/bin/activate && python3 -m pytest ...` — bare
  `python3 -m pytest` fails (`No module named pytest`) since pytest is only
  installed in the project venv, not system Python.
- **This dev machine has a REAL reachable Postgres with REAL seeded product
  data** — any test exercising a new code path that calls
  `_get_products_by_category`/`_load_*_offers` without mocking will silently
  hit it and can pass/fail depending on what's actually seeded, NOT on the
  code being correct. Discovered 2026-08-10 when a pre-existing dead-end
  test (`test_qualify.py::test_dead_end_no_offers`) passed as part of the
  full suite but FAILED in isolation once a new feature started actually
  querying microloan products from that DB (full-suite pass was luck — an
  earlier test's DB state left the connection unreachable by then). Always
  run a newly-DB-touching test in isolation (`pytest tests/test_x.py::test_y -v`)
  in addition to the full suite, and mock
  `app.agent.products._get_products_by_category` (or the specific
  `app.utils.data_loaders._load_*_offers`) explicitly rather than relying on
  "the DB happens to be unreachable" for determinism.

## Workflow Preferences (from CLAUDE.md, confirmed stable)
- Plan first for non-trivial tasks; commit messages suggested in Russian, never auto-committed
- New tools: add `@lc_tool` in `app/agent/tools.py` → register in `_FAQ_TOOLS` →
  if it changes dialog state, add a handler in `nodes/faq.py::_update_dialog_from_tools()`
- New product types: ORM model → Alembic migration → `CREDIT_SECTION_MAP` in
  `constants.py` → `_get_products_by_category()` in `products.py` → i18n → seed service
