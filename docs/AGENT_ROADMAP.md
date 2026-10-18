# План развития агента: от state-machine к полноценному LangGraph-агенту

Дата: 2026-09-04. Версии в `.venv`: langgraph 1.2.11, langgraph-checkpoint 4.2.0,
langgraph-checkpoint-postgres 3.1.2, langchain 1.3.17, langchain-core 1.6.0,
langchain-openai 1.6.0, langsmith 0.11.1.

Источник вердикта: аудит `app/agent/` и сверка с docs.langchain.com (разделы
`langchain/agents`, `langchain/middleware`, `langchain/tools`,
`langchain/short-term-memory`, `langchain/human-in-the-loop`, `langgraph/memory`,
`langgraph/streaming`, `langgraph/interrupts`, `langgraph/use-graph-api`,
`langchain/mcp`, `langsmith/observability-quickstart`).

Сводка: граф `router → faq|calc_flow|qualify_flow|human_mode|recap → END`
используется как диспетчер, а агентный цикл «модель → инструменты → модель»
написан вручную внутри `node_faq`. Из-за этого не работают чекпоинты между
шагами цикла, нативный стриминг, middleware, `Store` и runtime-контекст
инструментов. План ниже переводит агент на штатные примитивы LangGraph 1.x
без остановки продакшена.

## Порядок и этапы

| Этап | Пункты | Статус |
|------|--------|--------|
| 0. Быстрые победы | дефект профиля, трейсинг LangSmith | ✅ сделано 2026-09-04 |
| 1. Фундамент состояния | редьюсер `add_messages`, узел `prepare_turn` | ✅ сделано (Runtime/ToolRuntime перенесён в этап 2) |
| 2. Агентный подграф | `create_agent`, middleware | ✅ сделано за флагом `CONSULTANT_AGENT_V2` |
| 3. Память и стриминг | Store, стриминг из графа | ✅ сделано (Store за флагом `PROFILE_BACKEND`) |
| 4. HITL и evals | approve-gate, evals | ✅ сделано |
| 5. Оптимизация | скоуп инструментов, надёжность, MCP | ✅ сделано (MCP за флагом `MCP_ENABLED`) |

**Что осталось проверить на живой системе.** Всё новое поведение закрыто
флагами, выключенными по умолчанию, потому что модульные тесты не заменяют
прогон на реальной модели и реальной БД:

1. `CONSULTANT_AGENT_V2=true` — прогнать `scripts/eval_agent.py --live` и
   сравнить `tool_match` с базой на старом пути. Флаг снимать только после
   этого.
2. `PROFILE_BACKEND=store` — сначала `python3 scripts/migrate_profiles_to_store.py --apply`,
   затем сверить, что профиль известного клиента читается из Store.
3. `MCP_ENABLED=true` против mock-сервера (`chat-middleware-mock/mcp_server.py --http`),
   проверить, что запись в CRM встаёт на паузу и ждёт подтверждения.
4. `AGENT_TRACING_ENABLED=true` — убедиться, что в трейсах нет сырых
   телефонов и номеров карт.
5. `CONSULTANT_SUMMARIZATION=true` — отдельный прогон на длинном диалоге:
   меняет то, что видит модель.

Каждый этап заканчивается зелёными `tests/` и прогоном `scripts/diag_agent.py`
на том же наборе вопросов, что и до этапа. Ниже каждый пункт расписан в
формате: цель → что делаем → файлы → критерий готовности.

---

## П.0. Дефект: инструменты не получают профиль клиента (этап 0) — ✅ СДЕЛАНО

**Цель.** `affordability_check`, `recommend_product`, `select_product` должны
видеть `state.user_profile`, `lang`, `user_id`, `session_id`.

**Причина.** `ToolNode` вызывается с усечённым состоянием
(`app/agent/nodes/faq.py`, вызов `_FAQ_TOOL_NODE.ainvoke({"messages": ..., "dialog": ...})`),
а инструменты читают `state["user_profile"]` через `InjectedState`. В проде
там всегда `None`. Тесты не ловят, потому что передают профиль в инструмент напрямую.

**Что делаем.**
1. В `node_faq` передавать в `ainvoke` полный `state` с подменёнными `messages`:
   `{**state, "messages": loop_msgs, "dialog": dialog}`.
2. Добавить регрессионный тест в `tests/test_phase4_tools.py`: прогнать
   `node_faq` с моком LLM, который вызывает `affordability_check`, и проверить,
   что в ответе присутствует доход из профиля.
3. Проверить `_lang_from_state`: сейчас язык берётся из `dialog.last_lang`
   как запасной путь. После фикса он придёт напрямую из `state["lang"]`.

**Файлы.** `app/agent/nodes/faq.py`, `tests/test_phase4_tools.py`.

**Готово, когда.** Тест падает на старом коде и проходит на новом; в
`scripts/diag_agent.py` вопрос «потяну ли я платёж» с заполненным профилем
возвращает вердикт с учётом дохода.

---

## П.7а. Трейсинг LangSmith (этап 0) — ✅ СДЕЛАНО

**Цель.** Видеть каждый ход целиком: узлы, вызовы инструментов, токены,
стоимость, задержки, ошибки. Заменить ручной `token_usage` наблюдаемостью.

**Что делаем.**
1. `.env.example`: добавить `LANGSMITH_TRACING=false`, `LANGSMITH_API_KEY=`,
   `LANGSMITH_PROJECT=asaka-bot`, `LANGSMITH_ENDPOINT=` (для EU-региона).
2. `app/config.py`: ничего не менять, переменные читает сам SDK.
3. `Agent._ainvoke_locked`: передавать в `config` метаданные и теги, чтобы
   фильтровать в UI: `config["metadata"] = {"session_id", "user_id", "lang", "channel": "telegram"|"miniapp"}`,
   `config["tags"] = [lang, flow]`. Канал приходит из `ChatService`.
4. PII: `mask_pii` уже применяется к тексту до LLM, значит в трейсы попадает
   маскированный текст. Проверить, что `last_user_text` в состоянии тоже не
   светит сырой PII (сейчас сырой текст лежит в `state.last_user_text`; в трейс
   попадает как input узла). Решение: маскировать в `_ainvoke_locked` до
   `state_in` или включить `LANGSMITH_HIDE_INPUTS`/`HIDE_OUTPUTS` с кастомной
   функцией маскирования.
5. `docs/`: короткий раздел «как открыть трейс хода по session_id».

**Файлы.** `.env.example`, `app/agent/agent.py`, `app/services/chat_service.py`,
`CLAUDE.md` (раздел Environment Variables).

**Готово, когда.** В проекте LangSmith виден трейс тестового диалога с 3
инструментами, отфильтрованный по `session_id`; сырых номеров карт/паспортов в
трейсе нет.

---

## П.3. Редьюсер на `messages` и `Runtime`/`context_schema` (этап 1) — 🟡 ЧАСТИЧНО

**Цель.** Перестать читать весь чекпоинт перед каждым ходом и пересылать его
обратно целиком. Разделить «состояние диалога» и «контекст хода».

**Сейчас.** `BotState.messages: List[Any]` без редьюсера, поэтому
`Agent._ainvoke_locked` делает `aget_state` (с ретраями), потом собирает
`state_in` со всеми ключами и вызывает `ainvoke`. Два похода в Postgres на ход,
риск затирания при гонке (закрыт in-process локом, который не переживёт второй
воркер).

**Что делаем.**
1. `app/agent/state.py`:
   - `messages: Annotated[list[AnyMessage], add_messages]`.
   - `ui_blocks`, `keyboard_options`, `answer`, `show_operator_button`,
     `token_usage` оставить как перезаписываемые ключи хода (без редьюсера),
     но явно сбрасывать их в `router` (`Command(update={...})`), а не в `_ainvoke`.
   - Ввести `@dataclass TurnContext: session_id, user_id, lang, channel,
     user_profile` и передавать через `context_schema=TurnContext` при
     `StateGraph(BotState, context_schema=TurnContext)`.
2. `app/agent/agent.py`:
   - `_ainvoke_locked` перестаёт вызывать `_aload_existing_state`; на вход
     графа уходит только `{"messages": [HumanMessage(...)], "last_user_text": ..., "human_mode": ...}`
     плюс `context=TurnContext(...)`.
   - Языковая логика (`detect_language`, `check_lang_mismatch`,
     `lang_switch_offered`) переезжает в первый узел графа `prepare_turn`,
     потому что ей нужен `dialog` из чекпоинта. Узел возвращает
     `Command(update={"lang": ..., "dialog": ...}, goto="router")`.
   - `_aload_existing_state` остаётся только для `get_handoff_context` и
     `sync_history`.
3. `helpers._finalize_turn`: перестаёт собирать `msgs = prior + [Human, AI]`;
   возвращает только дельту `[HumanMessage(masked), AIMessage(answer)]`,
   а `add_messages` дописывает её сам. Обрезка истории (`trim_messages`)
   уезжает в middleware на этапе 2; до этого оставить `RemoveMessage` по
   бюджету токенов в `prepare_turn`.
4. Узлы получают `runtime: Runtime[TurnContext]` вторым аргументом и берут
   `session_id`/`user_id`/`lang`/`user_profile` из `runtime.context`.
   `state["lang"]`, `state["session_id"]`, `state["user_id"]`,
   `state["user_profile"]` помечаются как deprecated и удаляются в конце этапа.
5. Инструменты: `state: Annotated[dict, InjectedState]` заменить на
   `runtime: ToolRuntime[TurnContext]` (`langchain.tools.ToolRuntime`);
   `runtime.state["dialog"]`, `runtime.context.lang`, `runtime.context.user_profile`.
   Делать по одному инструменту, тесты `test_phase4_tools.py` адаптировать
   через фикстуру, которая строит `ToolRuntime`.
6. Миграция чекпоинтов: старые чекпоинты содержат полный список `messages`;
   `add_messages` их примет как есть (id проставит сам). Ключи
   `lang/session_id/user_id/user_profile` в старых чекпоинтах просто
   игнорируются. Отдельная миграция не нужна, но нужен тест
   «старый чекпоинт → новый ход не падает».
7. Флаг `AGENT_STATE_V2` не вводить: изменение атомарное, откат через git.

**Файлы.** `app/agent/state.py`, `app/agent/agent.py`, `app/agent/graph.py`,
`app/agent/nodes/*.py`, `app/agent/tools.py`, `app/agent/nodes/helpers.py`,
`tests/test_agent.py`, `tests/test_phase4_tools.py`, `tests/test_recap.py`.

**Готово, когда.** На ход приходится один `aget_tuple`+`aput` чекпоинтера
(проверить логом или счётчиком в тесте с `MemorySaver`), все тесты зелёные,
`diag_agent.py` даёт те же ответы.

**Статус на 2026-09-04.** Сделаны пункты 1 (частично), 2, 3, 6:
- `messages` получил редьюсер `add_messages`; `_finalize_turn` возвращает
  дельту, при исчерпании бюджета — полную замену через
  `RemoveMessage(REMOVE_ALL_MESSAGES)`, текущий ход всегда сохраняется.
- Языковая логика и загрузка профиля переехали в новый узел
  `nodes/prepare.py::node_prepare_turn` (точка входа графа). `_ainvoke_locked`
  больше не делает своего `aget_state` — один поход в чекпоинтер на ход,
  что проверяется тестом `tests/test_prepare_turn.py::TestCheckpointRoundTrips`.
- `BotState` получил `requested_lang` (вход) и `suggested_lang` (выход).
- Миграция старых чекпоинтов не потребовалась: `add_messages` сам проставляет
  id при первом слиянии (тест `TestLegacyCheckpointMigration`).
- Попутно исправлен предсуществующий дефект: `sync_history` падал с
  `Ambiguous update, specify as_node` на каждом вызове после первого, то есть
  история операторского чата не доходила до графа. Добавлен
  `as_node="human_mode"`.

**Осталось (перенесено в этап 2).** `TurnContext`/`context_schema`, `Runtime`
в узлах и переход инструментов с `InjectedState` на `ToolRuntime`. Причина
переноса: `create_agent` принимает `context_schema` напрямую, поэтому обе
правки касаются одних и тех же 16 инструментов и делаются одним проходом
вместе с П.1 вместо двух переписываний подряд. `InjectedState` в
langgraph 1.2.11 продолжает работать.

---

## П.1. `node_faq` → агентный подграф на `create_agent` (этап 2)

**Цель.** Убрать ручной цикл `for round in range(5)` и получить штатный
агент: чекпоинт после каждого шага, стриминг, middleware, `recursion_limit`.

**Что делаем.**
1. Новый модуль `app/agent/consultant.py`:
   ```python
   from langchain.agents import create_agent
   consultant = create_agent(
       model=_get_chat_openai("consultant"),
       tools=_FAQ_TOOLS,
       system_prompt=None,            # policy добавляет middleware (п.2)
       middleware=[...],              # см. п.2
       state_schema=BotState,         # общий state с родительским графом
       context_schema=TurnContext,
       name="consultant",
   )
   ```
   Подграф компилируется без своего чекпоинтера и вставляется в родительский
   `StateGraph` как узел `faq` (`graph.add_node("faq", consultant)`), чекпоинты
   наследуются от родителя.
2. Что остаётся в обёртке вокруг подграфа (узел `faq_pre` и `faq_post`, или
   `before_agent`/`after_agent` хуки middleware):
   - `faq_pre`: back-trigger «◀ Все продукты», строгий FAQ pre-check с
     `rephrase_faq_answer` (оба детерминированные, LLM не нужен).
   - `faq_post`: `_update_dialog_from_tools`, вход в `qualify_flow` после
     `get_products`, определение `is_fallback`/`is_ai_generated`, `_finalize_turn`.
   Оба берут список вызванных инструментов из `state["messages"]`
   (`AIMessage.tool_calls` за текущий ход), а не из локальной переменной.
3. «Display-tool short-circuit» (карточки продуктов/офисов уходят пользователю
   как есть, без пересказа моделью) реализуется штатно: у таких инструментов
   ставится `return_direct=True` (`@tool(return_direct=True)`), и агент
   завершает ход сразу после инструмента. Для `faq_lookup` со strict-ответом
   оставить `after_model`/`wrap_tool_call` middleware, который подменяет
   результат на `rephrase_faq_answer(...)`.
4. `parallel_tool_calls=False` остаётся (`model.bind_tools(..., parallel_tool_calls=False)`
   через `wrap_model_call`), потому что `_update_dialog_from_tools` смотрит на
   последний вызов.
5. `max_rounds=5` → `config["recursion_limit"]` на подграфе (примерно 2 шага на
   раунд, значит 12). Обработка `GraphRecursionError` в `faq_post` с прежней
   логикой «отдать последний полезный вывод инструмента».
6. Обработка `APIError` → `ModelFallbackMiddleware` (п.2); ветка
   «LLM недоступен → strict FAQ» остаётся в `faq_pre`.
7. Переключатель `CONSULTANT_AGENT_V2=true|false` на время перехода: `graph.py`
   регистрирует либо старый `node_faq`, либо новый подграф. Удалить флаг после
   двух недель на проде.

**Файлы.** новый `app/agent/consultant.py`, `app/agent/graph.py`,
`app/agent/nodes/faq.py` (ужимается до `faq_pre`/`faq_post`),
`app/agent/tools.py` (`return_direct`), `tests/test_agent.py`.

**Готово, когда.** `node_faq` короче 250 строк, `diag_agent.py` показывает
не хуже прежнего по доле «дошли до данных из БД», в LangSmith виден один
трейс хода с вложенным подграфом `consultant`.

---

## П.2. Middleware вместо самописных обёрток (этап 2, вместе с п.1)

**Цель.** Перенести сквозную логику в документированные хуки
`before_model`, `after_model`, `wrap_model_call`, `wrap_tool_call`.

**Карта переноса.**

| Сейчас (`nodes/faq.py`) | Куда | Заметка |
|---|---|---|
| `_format_state_xml` + `<client_profile>` + выбор `policy_mode` | `@before_model` → `StateInjectMiddleware` | Кладёт два `SystemMessage` в начало: policy (стабильный, для prompt-cache) и `<state>` |
| `mask_pii` перед LLM | `PIIMiddleware` (встроенный) или свой `wrap_model_call` | Своё регулярное выражение под UZ-паспорта/карты уже есть в `pii_masker.py`, подключить как `detector` |
| anti-loop `clarify` (`clarify_last_turn`) | `wrap_model_call` → `ClarifyGuardMiddleware` | Убирает `clarify` из `request.tools`, если прошлый ход был clarify |
| `_is_tool_error`, `handle_tool_errors` | `wrap_tool_call` → `ToolErrorMiddleware` | Возвращает `ToolMessage(status="error")`, модель видит, пользователь нет |
| `except APIError → _faq_lookup` | `ModelFallbackMiddleware(fallback_model)` + `faq_pre` | Вторая модель: `gpt-4o-mini` или Qwen |
| ретраи инструментов (нет) | `ToolRetryMiddleware(max_retries=2)` | Для `find_office`, `get_products`, `faq_lookup` (сеть/БД) |
| `trim_messages(MAX_DIALOG_TOKENS)` | `SummarizationMiddleware(model=extractor, trigger=("tokens", 12000), keep=("messages", 20))` | Длинный диалог сжимается, а не обрезается |
| стриминг через contextvar | убирается, см. п.5 | |

**Порядок.** Сначала `StateInjectMiddleware` и `ToolErrorMiddleware`
(без них подграф не работает), затем `ClarifyGuard`, затем
`ModelFallback`/`ToolRetry`, последним `Summarization` (меняет поведение
истории, отдельный прогон `diag_agent.py`).

**Файлы.** новый пакет `app/agent/middleware/` (`state_inject.py`,
`pii.py`, `clarify_guard.py`, `tool_errors.py`, `__init__.py`),
`app/agent/consultant.py`, тесты `tests/test_middleware.py`.

**Готово, когда.** Каждый middleware покрыт юнит-тестом на своём хуке без
LLM; регрессия `test_phase4_tools.py::test_clarify_anti_loop` проходит на
новой реализации.

---

## П.4. Долговременная память через `BaseStore` (этап 3)

**Цель.** Заменить самописную таблицу `UserProfile` + `profile.py` на штатный
`PostgresStore` с namespace, семантическим поиском и доступом из инструментов.

**Что делаем.**
1. `app/agent/checkpointer.py` → переименовать в `persistence.py`: рядом с
   `AsyncPostgresSaver` создавать `AsyncPostgresStore.from_conn_string(url, index={"dims": 1536, "embed": "openai:text-embedding-3-small"})`,
   `await store.setup()`. Для `memory` бэкенда `InMemoryStore`.
2. `graph.compile(checkpointer=..., store=store)`; `build_graph` уже принимает `store`.
3. Namespace: `("users", str(user_id), "profile")` с ключами `facts`, `notes`;
   `("users", str(user_id), "memories")` для отдельных фактов-эпизодов
   («в марте спрашивал про ипотеку на вторичку»), чтобы `store.search(ns, query=...)`
   находил релевантное к текущему вопросу.
4. `profile.py::load_user_profile` → `store.aget(ns, "facts")`; `upsert_user_profile`
   → `store.aput` (merge делаем в Python, JSONB `||` больше не нужен).
5. `memory_extract.py`: пишет в Store; фоновой запуск оставить как есть.
6. Инструменты: `affordability_check`/`recommend_product` читают
   `runtime.store` напрямую, `<client_profile>` в `before_model` берётся из
   `runtime.store` + `store.search` по тексту вопроса (топ-3 эпизода).
7. Миграция данных: одноразовый скрипт `scripts/migrate_profiles_to_store.py`
   переносит строки `user_profiles` в Store; таблицу и модель удалить через
   Alembic после проверки.
8. Админка: `UserProfile` ModelView заменить на простую страницу чтения Store
   по `user_id` (или временно оставить таблицу read-only).

**Файлы.** `app/agent/persistence.py`, `app/agent/profile.py`,
`app/agent/memory_extract.py`, `app/agent/graph.py`, `app/agent/tools.py`,
`app/admin/views.py`, `app/db/models.py` + миграция, `scripts/`,
`tests/test_profile.py`.

**Готово, когда.** Профиль после `/start` нового пользователя появляется в
Store, `affordability_check` находит доход через `runtime.store`, поиск
`store.search` возвращает эпизод по смыслу, старые профили перенесены.

---

## П.5. Стриминг через граф, а не contextvar (этап 3)

**Цель.** Убрать `app/agent/streaming.py` и ветку `_run_llm_round` с ручным
накоплением чанков; отдавать токены и UI-блоки Mini App штатным `astream`.

**Что делаем.**
1. `Agent.astream_turn(session_id, text, ...)` → `async for mode, chunk in graph.astream(input, config, context=..., stream_mode=["messages", "custom", "updates"], version="v2")`.
   - `messages`: пропускать только чанки с `metadata["langgraph_node"] == "consultant"`
     и `metadata.get("tags")` содержащим `"customer_facing"` (тег вешается в
     `wrap_model_call`, чтобы вызовы `extractor`/rerank не утекали в UI).
   - `custom`: UI-блоки. Инструменты с `content_and_artifact` дополнительно
     вызывают `get_stream_writer()({"ui_block": artifact})` в момент готовности,
     не дожидаясь конца хода.
   - `updates`: финальный `answer`, `keyboard_options`, `show_operator_button`.
2. `app/miniapp/routes/chat.py`: вместо `set_on_token_callback` перебирает
   `astream_turn` и публикует в `hub` события `assistant_token` /
   `ui_block` / `assistant_done`. Telegram продолжает вызывать `send_message`
   (внутри тот же `astream` с игнорированием токенов, чтобы код был один).
3. Удалить `app/agent/streaming.py`, `get_on_token_callback`, ветку
   `astream` в `_run_llm_round` (сам `_run_llm_round` исчезает с п.1).
4. Тест: `tests/test_ui_blocks.py` заменить мок contextvar на сбор чанков из
   `astream` с `FakeListChatModel`.

**Файлы.** `app/agent/agent.py`, `app/miniapp/routes/chat.py`,
`app/agent/tools.py`, `app/agent/streaming.py` (удалить), `docs/MINIAPP.md`.

**Готово, когда.** Mini App получает первый токен раньше, чем раньше (замер
в `diag_agent.py` по времени до первого события), UI-блок продукта
появляется до окончания хода, Telegram-путь не изменился по ответам.

---

## П.6. Human-in-the-loop через `HumanInTheLoopMiddleware` (этап 4)

**Цель.** Стандартизировать паузы графа: подтверждение чувствительных
действий, ожидание оператора, детект пендинг-interrupt.

**Что делаем.**
1. `request_operator` и будущие «identity-required» инструменты
   (`block_card`, `submit_application`) объявить в
   `HumanInTheLoopMiddleware(interrupt_on={"request_operator": {"allowed_decisions": ["approve", "reject"]}, ...})`.
   Payload `action_requests`/`review_configs` уходит в chat-middleware Asaka
   как первое сообщение оператору (сейчас это делает `handoff.py` руками).
2. `node_human_mode_turn` оставить для пассивного ожидания реплик оператора
   (это не approve-gate, а «спросить человека»), но:
   - определять «сессия ждёт оператора» через `(await graph.aget_state(config)).interrupts`
     (или `.tasks`), а не через `ChatSession.human_mode` + `state.human_mode`;
   - `resume_human_mode` → `Command(resume=...)` без изменений.
3. Демо-граф `examples/hitl_demo/` (уже сделан отдельно) использовать как
   спецификацию контрактов payload/resume при интеграции с оператором.
4. Документация `docs/CHAT_MIDDLEWARE_INTEGRATION.md`: раздел «как оператор
   подтверждает действие» с примером `Command(resume={"decisions": [...]})`.

**Файлы.** `app/agent/consultant.py`, `app/agent/nodes/human_mode.py`,
`app/agent/agent.py`, `app/services/chat_service.py`, `app/agent/handoff.py`,
`docs/CHAT_MIDDLEWARE_INTEGRATION.md`.

**Готово, когда.** Вызов `request_operator` останавливает граф до решения
оператора, `aget_state().interrupts` непустой, `resume` с `approve`
продолжает ход, с `reject` возвращает пользователю вежливый отказ.

---

## П.7б. Evals в CI на LangSmith (этап 4)

**Цель.** Проверять смену промпта/модели/middleware числом, а не глазами.

**Что делаем.**
1. Вопросы из `scripts/diag_agent.py` и `scripts/diag_model.py` оформить как
   dataset `asaka-bot-routing` в LangSmith: вход (текст, lang, стартовый
   dialog), ожидание (ожидаемый инструмент, категория, «дошли до данных из БД»).
2. `scripts/eval_agent.py`: `langsmith.evaluate(target=run_turn, data=dataset, evaluators=[tool_match, reached_db, no_pii_leak, latency_under(8s)])`.
3. GitHub Actions job `evals` по ручному триггеру и по метке PR `run-evals`
   (стоит денег, автоматически на каждый пуш не гонять).
4. Порог: доля `tool_match` не ниже текущего значения минус 2 п.п.

**Файлы.** `scripts/eval_agent.py`, `.github/workflows/evals.yml`, `docs/EVALS.md`.

**Готово, когда.** Один запуск даёт таблицу по evaluator'ам и ссылку на
эксперимент; PR с намеренно сломанным docstring `get_products` проваливает порог.

---

## П.9. Стоимость контекста (этап 5)

**Цель.** Сократить ~12 500 токенов постоянного префикса (9 000 схемы
инструментов + 3 500 policy) без потери точности маршрутизации.

**Что делаем.**
1. Измерить базу: `diag_model.py` на текущем наборе, зафиксировать
   `tool_match` и средний `prompt_tokens`.
2. Динамический набор инструментов в `wrap_model_call`:
   - `what_if_scenario`, `affordability_check`, `start_calculator`,
     `compare_products` только при `dialog.selected_product` или `dialog.products`;
   - `select_office` только при `dialog.offices`;
   - `select_product` только при `dialog.products`.
3. Few-shot примеры из docstring'ов (по 10-30 строк на инструмент, на 3 языках)
   перенести в dataset evals (п.7б) и оставить в описании 3-5 самых
   показательных. Проверять `tool_match` после каждого сокращения.
4. Policy: вынести редко нужные разделы (правила выбора офиса уже вынесены в
   `office_select`) в режимные добавки; базу довести до ~2 000 токенов.
5. `SummarizationMiddleware` (п.2) закрывает рост истории.

**Файлы.** `app/agent/tools.py`, `app/agent/i18n.py`,
`app/agent/middleware/tool_scope.py`, `scripts/diag_model.py`.

**Готово, когда.** Средний `prompt_tokens` хода ниже на 30% при `tool_match`
не хуже базы минус 1 п.п.

---

## П.10. Мелочи надёжности (этап 5)

1. **RetryPolicy на узлах с сетью.** `graph.add_node("faq", ..., retry_policy=RetryPolicy(max_attempts=2, retry_on=(APIConnectionError, OperationalError)))`,
   то же для `calc_flow` (extractor LLM) и `qualify_flow`.
2. **Таймаут хода.** `asyncio.wait_for(..., AGENT_TIMEOUT_SECONDS)` в
   `ChatService` отменяет граф посреди записи чекпоинта. Заменить на
   `graph.ainvoke(..., durability="sync")` (чекпоинт пишется до перехода к
   следующему узлу) плюс таймауты на уровне модели (`OPENAI_REQUEST_TIMEOUT`
   уже есть) и `recursion_limit`. `wait_for` оставить как последний рубеж с
   запасом (модельный таймаут × число раундов + 5с), а не 25с на всё.
3. **Per-session lock.** Заменить `_session_locks` на Redis-лок
   (`redis.asyncio.lock`, TTL = таймаут хода) за флагом `SESSION_LOCK_BACKEND=memory|redis`,
   чтобы можно было поднять второй воркер. Redis уже нужен для rate-limit
   middleware при масштабировании.
4. **Idempotency webhook.** Дедуп `update_id` в `POST /telegram/webhook`
   (кэш последних 1000 id в памяти или Redis), чтобы повторная доставка не
   порождала второй ход.
5. **Проверка чекпоинтера при старте.** Уже есть (`REQUIRE_PERSISTENT_CHECKPOINTER`);
   добавить в `/health` проверку `store` после п.4.

**Файлы.** `app/agent/graph.py`, `app/services/chat_service.py`,
`app/agent/agent.py`, `app/api/fastapi_app.py`, `app/config.py`.

---

## П.8. MCP (по необходимости, этап 5)

**Цель.** Подключать внешние сервисы банка (CRM, скоринг, курсы, статус
заявки) как MCP-серверы, а не как код в `tools.py`.

**Что делаем.**
1. `pip install langchain-mcp-adapters` (или `langchain[mcp]` ≥ 1.4 с
   `MCPAdapter`, когда выйдет из беты).
2. `app/agent/mcp_tools.py`:
   ```python
   client = MultiServerMCPClient({
       "crm": {"url": settings.mcp_crm_url, "transport": "http",
               "headers": {"Authorization": f"Bearer {settings.mcp_crm_token}"}},
   })
   mcp_tools = await client.get_tools()
   ```
   Загружать один раз в `lifespan`, класть в `middleware_registry`-подобный
   реестр, добавлять к `_FAQ_TOOLS` при сборке `create_agent`.
3. Чувствительные MCP-инструменты (запись в CRM) сразу заводить в
   `HumanInTheLoopMiddleware.interrupt_on` (п.6).
4. Для локальной разработки: `chat-middleware-mock/` расширить mock MCP-сервером
   на FastMCP с двумя инструментами (`get_application_status`, `create_application`).
5. Обратное направление (свои инструменты как MCP-сервер для других систем
   банка) через `langchain_mcp_adapters.tools.to_fastmcp`, если понадобится.

**Файлы.** `requirements.txt`, `app/agent/mcp_tools.py`, `app/config.py`,
`app/api/fastapi_app.py`, `chat-middleware-mock/`.

**Готово, когда.** Агент вызывает `get_application_status` из mock MCP-сервера
в `diag_agent.py`, запись в CRM проходит через approve-gate.

---

## Что НЕ делаем

- Не переписываем `calc_flow`, `qualify_flow`, `recap`: детерминированные узлы
  работают и покрыты тестами; они остаются узлами родительского графа.
- Не меняем протокол с Asaka chat-middleware и Mini App API.
- Не переносим FAQ-поиск (Weaviate + rerank) в Store: это отдельный RAG-слой.
- Не вводим multi-agent (supervisor/swarm): один consultant-агент плюс
  детерминированные узлы покрывают домен; по документации subagents оправданы
  при нескольких независимых доменах и параллельной работе, чего здесь нет.

## Чек-лист перед каждым этапом

1. `python3 -m pytest tests/ -v` зелёный.
2. `scripts/diag_agent.py` на фиксированном наборе, результат сохранён в
   `docs/evals/<дата>.md`.
3. Прогон 10 диалогов в Telegram и Mini App по чек-листу из `docs/MINIAPP.md`.
4. Обновлены `CLAUDE.md` (архитектура, env) и этот файл (отметка «сделано»).
