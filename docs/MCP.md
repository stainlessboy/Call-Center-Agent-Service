# MCP: внешние сервисы банка как инструменты агента

Реализация пункта **П.8** из [AGENT_ROADMAP.md](AGENT_ROADMAP.md).
Код: `app/agent/mcp_tools.py`, mock-сервер: `chat-middleware-mock/mcp_server.py`,
тесты: `tests/test_mcp_tools.py`.

## Зачем это нужно

CRM, скоринг, статус заявки и курсы принадлежат другим командам. Сейчас каждый
такой вызов пришлось бы писать руками как `@lc_tool` в `app/agent/tools.py` —
и вместе с ним тащить в этот репозиторий чужую схему данных, чужую авторизацию
и чужие ломающие изменения.

MCP (Model Context Protocol) переворачивает зависимость: сервис-владелец
поднимает MCP-сервер и сам описывает свои инструменты, а бот **обнаруживает их
на старте**. Подключение нового сервиса становится изменением переменной
окружения, а не релизом бота.

Что это даёт конкретно здесь:

- инструменты `get_application_status`, `create_application`, `get_scoring`
  и любые будущие появляются у агента без правок `tools.py`;
- схемы аргументов приходят от сервера, значит не расходятся с реальным API;
- падение внешнего сервиса деградирует бота до «бот без этих инструментов», а
  не до сломанного бота (см. «Отказоустойчивость»).

## Какой клиент используется

`langchain_mcp_adapters.client.MultiServerMCPClient` (пакет
`langchain-mcp-adapters`, версия 0.3.2).

Роадмап допускал альтернативу — `langchain.mcp.MCPAdapter` из `langchain ≥ 1.4`.
В установленном `langchain 1.3.17` модуля `langchain.mcp` **нет**
(`ModuleNotFoundError`), поэтому выбран `MultiServerMCPClient`. Проверить можно
так:

```bash
.venv/bin/python -c "import langchain.mcp"   # ModuleNotFoundError на 1.3.17
```

Поддерживаемые транспорты (по `langchain_mcp_adapters.sessions.create_session`):
`stdio`, `sse`, `websocket` и streamable HTTP. Для HTTP библиотека принимает три
написания — `http`, `streamable-http`, `streamable_http`; наш `_server_config()`
нормализует их все к каноничному `streamable_http`, чтобы опечатка в env
превращалась в понятный warning, а не в `ValueError` внутри сессии.

## Переменные окружения

| Переменная | По умолчанию | Назначение |
|---|---|---|
| `MCP_ENABLED` | `false` | Главный выключатель. Пока `false` — ни одного сетевого вызова, `load_mcp_tools()` сразу возвращает `[]`. |
| `MCP_SERVERS` | — | JSON-объект `{"имя": {конфиг соединения}}`. Основной способ конфигурации. |
| `MCP_CRM_URL` | — | Сокращение для частого случая «один сервер»: создаёт сервер с именем `crm` и транспортом streamable HTTP. |
| `MCP_CRM_TOKEN` | — | Токен к `MCP_CRM_URL`, уходит как заголовок `Authorization: Bearer <токен>`. |
| `MCP_LOAD_TIMEOUT_SECONDS` | `15` | Ограничение на весь этап обнаружения инструментов, чтобы недоступный сервер не подвесил старт приложения. |

Если один и тот же сервер `crm` описан и в `MCP_SERVERS`, и через `MCP_CRM_URL`,
побеждает явная запись из `MCP_SERVERS` (она более осознанная), в лог пишется
сообщение уровня INFO.

### Примеры

Один сервер, короткая форма:

```env
MCP_ENABLED=true
MCP_CRM_URL=https://crm.asakabank.uz/mcp
MCP_CRM_TOKEN=eyJhbGciOi...
```

Несколько серверов, явная форма (JSON в одну строку):

```env
MCP_ENABLED=true
MCP_SERVERS={"crm": {"url": "https://crm.asakabank.uz/mcp", "transport": "http", "headers": {"Authorization": "Bearer eyJ..."}}, "scoring": {"url": "https://scoring.asakabank.uz/mcp", "transport": "http"}}
```

Локальный процесс через stdio (бот сам запускает сервер, порт не нужен):

```env
MCP_ENABLED=true
MCP_SERVERS={"crm": {"command": "/abs/path/.venv/bin/python", "args": ["/abs/path/chat-middleware-mock/mcp_server.py"], "transport": "stdio"}}
```

Поля записи: `transport` (можно опустить — выводится из наличия `url` или
`command`), `url` (для `http`/`sse`/`websocket`), `command` + `args` (для
`stdio`), `headers` (объект). `args` для `stdio` можно не указывать — подставится
`[]`, без этого библиотека бросила бы `ValueError`.

## Mock-сервер для разработки

`chat-middleware-mock/mcp_server.py` — рабочий MCP-сервер на FastMCP с
детерминированными данными (одинаковые аргументы всегда дают одинаковый ответ,
никаких внешних вызовов). Инструменты:

- `get_application_status(application_id)` — статус заявки, чтение;
- `create_application(product, amount, term_months, phone)` — **запись**;
- `get_scoring(passport, income)` — скоринговый вердикт, чтение.

Запуск в режиме HTTP (то, что использует бот):

```bash
.venv/bin/python chat-middleware-mock/mcp_server.py --http
# слушает http://127.0.0.1:8765/mcp
# свой адрес: --host 0.0.0.0 --port 9000
```

Запуск в режиме stdio:

```bash
.venv/bin/python chat-middleware-mock/mcp_server.py
```

Направить бота на него:

```env
MCP_ENABLED=true
MCP_CRM_URL=http://127.0.0.1:8765/mcp
```

Проверить связку без бота (сервер должен быть запущен):

```bash
MCP_ENABLED=true MCP_CRM_URL=http://127.0.0.1:8765/mcp .venv/bin/python -c "
import asyncio
from app.agent.mcp_tools import load_mcp_tools
print([t.name for t in asyncio.run(load_mcp_tools())])
"
# ['get_application_status', 'create_application', 'get_scoring']
```

## Подключение в приложении

Модуль читает окружение сам (через `os.getenv`, как `app/agent/tracing.py`) и не
трогает `app/config.py`. Реестр повторяет схему
`app/services/middleware_registry.py`: инструменты грузятся один раз на старте и
читаются агентом без импорта FastAPI.

```python
from app.agent.mcp_tools import load_mcp_tools, set_mcp_tools, aclose

# lifespan, startup
set_mcp_tools(await load_mcp_tools())

# lifespan, shutdown
await aclose()
```

И там, где собирается список инструментов агента:

```python
from app.agent.mcp_tools import get_mcp_tools

tools = [*_FAQ_TOOLS, *get_mcp_tools()]
```

`get_mcp_tools()` возвращает копию списка, поэтому вызывающий код не может
случайно испортить реестр.

## Отказоустойчивость

Главное свойство модуля: **ни одна функция не бросает исключение наружу**.
Вызывающая сторона — это старт приложения и сборка списка инструментов агента,
поэтому недоступный MCP-сервер обязан деградировать до «бот без этих
инструментов», а не до неподнимающегося бота.

`load_mcp_tools()` возвращает `[]` и пишет warning, когда:

- `MCP_ENABLED` не выставлен (это штатный случай, пишется в DEBUG);
- ни `MCP_SERVERS`, ни `MCP_CRM_URL` не заданы;
- `MCP_SERVERS` содержит невалидный JSON или не объект;
- пакет `langchain-mcp-adapters` не установлен;
- сервер недоступен, вернул ошибку или не ответил за `MCP_LOAD_TIMEOUT_SECONDS`.

Одна битая запись в `MCP_SERVERS` не отменяет остальные: невалидный сервер
пропускается с warning, валидные подключаются.

## Инструменты записи и approve-gate

`SENSITIVE_MCP_TOOLS` в `app/agent/mcp_tools.py` — frozenset имён инструментов,
которые **меняют состояние банковских систем** и потому требуют подтверждения
человеком: `create_application`, `submit_application`, `update_application`,
`cancel_application`, `block_card`, `unblock_card`, `transfer`, `transfer_funds`.

Набор объявлен рядом с загрузчиком намеренно — чтобы список не разъезжался с
инструментами, которые он описывает. Проверять принадлежность нужно через
`is_sensitive_tool(name)`: она нечувствительна к регистру и понимает префикс
сервера, который добавляет `MultiServerMCPClient(tool_name_prefix=True)`
(`crm_create_application` тоже считается чувствительным).

Пока это **инертные метаданные**: сам по себе импорт ничего не блокирует, так
что случайно закрыть инструмент нельзя. Гейт включается на этапе П.6 роадмапа:

```python
from langchain.agents.middleware import HumanInTheLoopMiddleware
from app.agent.mcp_tools import get_mcp_tools, is_sensitive_tool

gated = {
    t.name: {"allowed_decisions": ["approve", "reject"]}
    for t in get_mcp_tools() if is_sensitive_tool(t.name)
}
middleware = [HumanInTheLoopMiddleware(interrupt_on=gated)]
```

После этого вызов `create_application` останавливает граф `interrupt()`-ом,
запрос уходит оператору в чат-middleware Asaka, а `Command(resume=...)` с
решением `approve` продолжает ход или с `reject` возвращает пользователю отказ.
Подробности контракта — в
[CHAT_MIDDLEWARE_INTEGRATION.md](CHAT_MIDDLEWARE_INTEGRATION.md).

## Как добавить новый MCP-сервер

1. Узнать у команды-владельца URL MCP-эндпоинта и способ авторизации.
2. Добавить запись в `MCP_SERVERS` (имя сервера — короткое и латиницей, оно
   может попасть в префикс имён инструментов):
   ```env
   MCP_SERVERS={"crm": {...}, "fx": {"url": "https://fx.asakabank.uz/mcp", "transport": "http"}}
   ```
3. Перезапустить приложение и проверить лог старта:
   `Loaded N MCP tool(s) from M server(s): ...`.
4. Если у сервера есть инструменты записи — добавить их имена в
   `SENSITIVE_MCP_TOOLS`, иначе они пойдут в обход подтверждения оператором.
5. Убедиться, что docstring'и инструментов на стороне сервера внятные: именно по
   ним LLM решает, когда их вызывать (то же правило, что и для собственных
   инструментов в `tools.py`).

Имена инструментов не должны конфликтовать с уже существующими 16 инструментами
агента. Если конфликт возможен, поднимать клиента с `tool_name_prefix=True` —
тогда имена станут вида `crm_create_application`; `is_sensitive_tool()` это
учитывает.

## Обратное направление

Если понадобится отдать собственные инструменты бота другим системам банка как
MCP-сервер — это делается через `langchain_mcp_adapters.tools.to_fastmcp`.
Сейчас не реализовано.

## Тесты

```bash
.venv/bin/python -m pytest tests/test_mcp_tools.py -v
```

Сеть не используется: тесты либо проверяют чистый разбор конфигурации, либо
мокают `MultiServerMCPClient`. Покрыты разбор `MCP_SERVERS` (валидный,
поломанный, частично поломанный), короткая форма `MCP_CRM_*`, реестр,
возврат `[]` при выключенном MCP / падении клиента / таймауте, и набор
чувствительных инструментов.
