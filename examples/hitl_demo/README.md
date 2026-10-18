# HITL-демо на LangGraph — «Кредитная заявка с участием человека»

Автономный, изолированный от `app/` пример графа на **LangGraph 1.x**,
демонстрирующий все ключевые паттерны Human-in-the-Loop (HITL). Ничего не
трогает продакшн-код проекта, не требует БД, Weaviate или Telegram — только
`langgraph`/`langchain-core` (и опционально `langchain-openai`, если включить
`--llm`).

## Что такое HITL в LangGraph

Human-in-the-Loop — это возможность **приостановить** выполнение графа внутри
узла, отдать управление человеку (или другой системе), а затем **продолжить**
выполнение ровно с того места, где остановились, с учётом ответа человека.
Работает это на трёх китах:

1. **`checkpointer`** — сохраняет полное состояние графа (все каналы + очередь
   задач) на каждом шаге. Без чекпоинтера `interrupt()` работать не будет —
   ведь после паузы процесс может даже перезапуститься, а состояние должно
   пережить это. В демо используется `InMemorySaver` (только для примера — в
   `app/agent/checkpointer.py` продакшн-бот использует Postgres-чекпоинтер).
2. **`langgraph.types.interrupt(value)`** — вызывается ВНУТРИ узла. При первом
   вызове в рамках текущей задачи бросает `GraphInterrupt`, отдаёт `value`
   наружу (в `__interrupt__` результата `invoke()`) и весь граф встаёт на
   паузу. При повторном запуске (resume) **весь узел выполняется заново с
   начала**, но на этот раз `interrupt()` не бросает исключение, а сразу
   возвращает значение, переданное при возобновлении.
3. **`langgraph.types.Command(resume=...)`** — передаётся вместо обычного
   входного состояния в `graph.invoke()`/`graph.stream()`, чтобы возобновить
   выполнение с последнего чекпоинта конкретного `thread_id`.

Важное следствие пункта 2 — **идемпотентность**: любой код узла ДО вызова
`interrupt()` выполнится дважды (один раз до паузы, второй раз после
возобновления). Если это чтение/парсинг — не страшно. Если это побочный
эффект (запись в БД, отправка запроса) — его нужно либо защищать от повтора,
либо, что проще и надёжнее, **выносить в отдельный узел после
`interrupt()`** (см. `submit_application` ниже).

## 5 паттернов HITL в этом графе

| # | Узел | Что показывает | Payload `interrupt()` | Формат `resume` |
|---|------|------------------|------------------------|-------------------|
| 1 | `intake` | запрос недостающих данных у пользователя, с возвратом на тот же узел через **условное ребро** (а не `while` внутри узла) | `{"type": "ask_user", "question": ..., "missing": [...]}` | свободный текст, парсится в конкретное поле |
| 2 | `verify_document` | **параллельные** interrupt'ы (через `Send`), возобновляемые ОДНОЙ командой | `{"type": "verify_document", "document": ...}` — по одному на документ, одновременно | `Command(resume={interrupt.id: True/False, ...})` — словарь, ключи — `Interrupt.id` |
| 3 | `review_offer` | review / edit / reject с обратной связью, `Command(goto=...)` прямо из узла | `{"type": "review", "draft": ..., "allowed_decisions": [...]}` | `{"decision": "approve"}` \| `{"decision": "edit", "edited_text": ...}` \| `{"decision": "reject", "feedback": ...}` |
| 4 | `approval_gate` | approval-gate для вызова инструмента — форма payload'а повторяет `HumanInTheLoopMiddleware` из `langchain.agents` | `{"type": "action_request", "action_requests": [...], "review_configs": [...]}` | `{"decisions": [{"type": "approve"}]}` \| `{"type": "edit", "edited_action": {...}}` \| `{"type": "reject", "message": ...}` |
| 5 | `operator_handoff` | передача живому оператору, ожидание **без таймаута** | `{"type": "operator", "summary": ...}` | произвольный текст ответа оператора |

Полная схема маршрутизации:

```
START → intake ⇄ intake (пока не хватает полей)
      → risk_check → Send(verify_document) × N (параллельно)
      → collect_verification (fan-in)
      → operator_handoff  [если риск high ИЛИ документ не подтверждён]
      → draft_offer → review_offer
           ├─ approve/edit → approval_gate → submit_application → notify
           └─ reject (< 3 раз) → draft_offer (заново)
              reject (3-й раз) → operator_handoff → notify
      approval_gate ├─ approve/edit → submit_application → notify
                     └─ reject → notify
      operator_handoff → notify → END
```

В коде намеренно показаны **оба стиля маршрутизации**:
`add_conditional_edges` (функция-роутер отдельно от узла — см.
`route_after_intake`, `route_after_verification`) и `Command(goto=...)`
прямо из узла, когда маршрут зависит от ответа человека (`risk_check`,
`review_offer`, `approval_gate`).

## Идемпотентность и побочные эффекты — правило проекта

`submit_application` — единственный узел с реальным побочным эффектом
(запись в `FAKE_CRM`). Он **не** вызывает `interrupt()` сам — approve
происходит в `approval_gate`, а запись в CRM — уже в следующем узле. Если бы
запись в CRM была частью того же узла, что и `interrupt()`, при
возобновлении граф заново выполнил бы ВЕСЬ узел с начала — включая повторную
запись. Разделение «спросить разрешение» / «сделать» — стандартный способ
избежать дублирования побочных эффектов в HITL-графах.

## Как обнаружить незавершённый interrupt

Два эквивалентных способа:

```python
result = graph.invoke(state, config)
if result.get("__interrupt__"):
    for interrupt in result["__interrupt__"]:
        print(interrupt.value, interrupt.id)
```

```python
snapshot = graph.get_state(config)
print(snapshot.next)          # какие узлы выполнятся следующими
print(snapshot.interrupts)    # активные Interrupt для этого чекпоинта
```

Второй способ полезен, когда вы не держите результат последнего `invoke()`
под рукой (например, отдельный HTTP-запрос «есть ли pending-вопрос у этой
сессии»).

## Запуск

```bash
# из корня репозитория, с активным venv проекта
source .venv/bin/activate

# интерактивный режим — вопросы задаются в stdin
python -m examples.hitl_demo.run_demo

# полностью автоматический прогон (ask_user → parallel verify → reject → edit → approval edit → submit)
python -m examples.hitl_demo.run_demo --auto

# вариант с высоким риском (риск-менеджер, эскалация оператору сразу после проверки документов)
python -m examples.hitl_demo.run_demo --auto --high-risk

# демонстрация time-travel: полный прогон + get_state_history + форк истории
python -m examples.hitl_demo.run_demo --time-travel

# вывести Mermaid-диаграмму графа
python -m examples.hitl_demo.run_demo --mermaid

# позволить draft_offer использовать OpenAI (нужен OPENAI_API_KEY в окружении)
python -m examples.hitl_demo.run_demo --auto --llm
```

Без `OPENAI_API_KEY` и без флага `--llm` всё работает **полностью офлайн** —
`draft_offer` использует детерминированный шаблон текста предложения. Если
флаг `--llm` передан, но вызов `ChatOpenAI` падает по любой причине (нет
ключа, сеть недоступна, квота исчерпана) — узел молча откатывается на
шаблон (`try/except` в `draft_offer`), демо не ломается.

> Примечание про `--mermaid`: рёбра, которые узел выбирает динамически через
> `Command(goto=...)` (`risk_check`, `review_offer`, `approval_gate`),
> `draw_mermaid()` не может вывести из кода. Чтобы они попали на диаграмму,
> узлы объявляют возможные цели в аннотации возвращаемого типа:
> `-> Command[Literal["submit_application", "notify"]]`. Альтернатива с тем
> же эффектом: `add_node(..., destinations=("submit_application", "notify"))`.

## Time-travel

`--time-travel` показывает:

1. `graph.get_state_history(config)` — список всех чекпоинтов треда (от
   новых к старым), у каждого `.config` (можно возобновить/форкнуть именно
   с этой точки) и `.next` (какие узлы выполнятся дальше).
2. `graph.update_state(checkpoint_config, values)` — записывает новые
   значения каналов ПОВЕРХ прошлого чекпоинта, создавая **новый** чекпоинт —
   форк, а не перезапись истории.
3. `graph.invoke(None, forked_config)` — возобновляет выполнение с форкнутого
   чекпоинта (передаём `None`, а не `Command(resume=...)`, так как этот
   чекпоинт не стоит на паузе `interrupt()` — следующий узел просто ещё не
   выполнялся).

После форка исходная история треда остаётся нетронутой — форк лишь
добавляет новую ветку чекпоинтов поверх выбранной точки.

## Тесты

```bash
.venv/bin/python -m pytest tests/test_hitl_demo.py -v
```

13 тестов проходят весь сценарий скриптованными `Command(resume=...)` —
включая happy path, все 5 паттернов по отдельности, эскалацию по риску,
эскалацию после 3 отклонений ревью и независимость `thread_id` друг от
друга. Сеть не используется.

## Как это соотносится с продакшн-ботом

- **PATTERN 5 (`operator_handoff`)** — прямой аналог
  `app/agent/nodes/human_mode.py::node_human_mode_turn`: там `interrupt()`
  точно так же ставит граф на паузу до ответа оператора, только ответ
  приходит не через stdin, а по Socket.IO из Asaka chat-middleware
  (`_on_agent_message` в `app/api/fastapi_app.py` → `Command(resume=...)`
  через `AgentClient.resume_human_mode`).
- Инструмент `request_operator` в `app/agent/tools.py` — LLM-триггер того же
  самого перехода в human_mode, только решение «нужен ли оператор» в
  продакшене принимает LLM, а не заранее заданное правило (`risk_level == "high"`
  или 3 отклонения ревью, как в этом демо).
- **PATTERN 1 (`intake` / ask_user)** в продакшене не реализован через
  `interrupt()` — там LLM в `node_faq` сам решает, что переспросить,
  оставаясь в рамках одного хода диалога (без паузы графа). `interrupt()`
  оправдан именно тогда, когда пауза должна пережить процесс/чекпоинт (как
  `human_mode`), а не когда достаточно ответа в том же ходе.

## Чем заменить это в `create_agent` (LangChain 1.x)

Если бы этот сценарий строился поверх `langchain.agents.create_agent`
(готовый ReAct-агент, а не ручной `StateGraph`), паттерны 3 и 4
(review/edit/reject черновика и approval-gate для вызова инструмента)
покрывались бы одним встроенным middleware:

```python
from langchain.agents import create_agent
from langchain.agents.middleware import HumanInTheLoopMiddleware

agent = create_agent(
    model="gpt-4o-mini",
    tools=[submit_application_tool],
    middleware=[
        HumanInTheLoopMiddleware(
            interrupt_on={
                "submit_application": {
                    "allowed_decisions": ["approve", "edit", "reject"],
                }
            },
        )
    ],
)
```

`HumanInTheLoopMiddleware` сама вызывает `interrupt()` перед выполнением
инструмента `submit_application` с payload'ом ровно той формы, которую
воспроизводит `approval_gate` в этом демо (`action_requests`/
`review_configs`) — это не совпадение, демо намеренно копирует контракт
миддлвари, чтобы колеги видели: «ручной» `StateGraph`-паттерн и готовое
middleware — это один и тот же примитив `interrupt()`, просто с разным
объёмом кода вокруг. Паттерны 1, 2 и 5 (запрос свободных данных, параллельная
проверка, бессрочный хендофф оператору) специфичны для этого сценария и
middleware их не покрывает — под них в любом случае нужен свой узел с
`interrupt()`, как в этом демо.
