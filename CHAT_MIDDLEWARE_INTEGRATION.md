# Интеграция Bot ↔ Chat Middleware (Cisco UCCX)

Архитектура интеграции Telegram-бота с Chat Middleware System (Cisco UCCX CCP) для передачи диалогов живым операторам.

---

## Текущее состояние

При нажатии "👤 Живой оператор" бот рассылает уведомление всем операторам из `OPERATOR_IDS` в Telegram. Нет очереди, нет автоматического назначения.

## Целевое состояние

При переходе в режим оператора бот подключается к Chat Middleware через Socket.IO. Cisco UCCX автоматически ставит в очередь и назначает свободного оператора. Оператор общается через Cisco CCP.

---

## Общая схема

```
┌──────────────┐         ┌──────────────────────────────┐         ┌─────────────────────┐
│   Telegram   │ ──────► │        Bot (FastAPI)          │         │  Chat Middleware     │
│    User      │ ◄────── │                                │ ◄─────►│  (Cisco UCCX CCP)   │
└──────────────┘         │  ┌──────────────────────────┐ │ SIO    │                     │
                         │  │  Socket.IO Client        │─┼────────►│  JWT auth           │
                         │  │  (python-socketio)       │◄┼────────│  Очередь            │
                         │  └──────────────────────────┘ │         │  Назначение         │
                         │                                │         │  Таймауты           │
                         │  LangGraph Agent (как сейчас)  │         └─────────────────────┘
                         └──────────────────────────────┘                    │
                                                                            ▼
                                                                    ┌──────────────┐
                                                                    │  Оператор    │
                                                                    │  (Cisco CCP) │
                                                                    └──────────────┘
```

**Ключевой принцип:** очередь, назначение оператора и таймауты — всё управляется Cisco UCCX. Бот только подключается через Socket.IO и слушает события.

---

## Протокол общения

### Способ связи: Socket.IO

Постоянное соединение (не отдельные HTTP запросы). Одно соединение = один чат с оператором.

```
Bot → Middleware (emit)                  Middleware → Bot (on event)
──────────────────────                  ────────────────────────────
connect(token=JWT)
                                        chat-event: {type: "chat_initialized"}
emit("start-chat", {csq, message})
                                        chat-event: {type: "chat_accepted"}
                                        chat-event: {type: "chat_agent_joined", agent: "Алина"}
emit("send-message", {message})
                                        chat-event: {type: "chat_message", from: "agent", text: "..."}
emit("send-leave", {})
                                        chat-event: {type: "chat_left"}
                                        disconnect

Ошибки:
                                        channel-errors: {code: "chat_request_rejected_by_agent"}
                                        channel-errors: {code: "chat_timedout_waiting_for_agent"}
```

### Socket.IO каналы

| Канал | Направление | Назначение |
|-------|-------------|------------|
| `start-chat` | Bot → Middleware | Начать чат (встать в очередь) |
| `send-message` | Bot → Middleware | Отправить сообщение оператору |
| `send-leave` | Bot → Middleware | Завершить чат со стороны клиента |
| `chat-event` | Middleware → Bot | Все события чата (статусы, сообщения) |
| `channel-errors` | Middleware → Bot | Ошибки (все заняты, таймаут) |

---

## Пошаговый цикл чата

### Шаг 1: Авторизация (JWT)

Перед подключением нужно получить токен. У каждого канала (Telegram-бот, сайт и т.д.) свои учётные данные.

```
Bot → POST https://chat-middleware.asakabank.uz/api/users/login
      body: {"login": "telegram_bot", "password": "***"}

Middleware → {"token": "eyJhbG..."}     ← живёт 3 часа
```

### Шаг 2: Подключение по Socket.IO

```python
sio.connect(
    url="https://chat-middleware.asakabank.uz",
    socketio_path="/api/ws/chats/",
    transports=["websocket"],       # только websocket, не polling
    auth={"token": "eyJhbG..."},    # JWT в параметрах
)
# Версия протокола: EIO=4
```

После подключения бот подписывается на `chat-event` и `channel-errors`.

### Шаг 3: Начать чат (встать в очередь)

```python
sio.emit("start-chat", {
    "csq": "Chat_Queue_1",                          # Contact Service Queue в Cisco
    "message": "Клиент интересовался ипотекой",      # контекст для оператора
    "customerName": "@username",                     # имя клиента
})
```

**Что делает Cisco UCCX:**
1. Получил запрос
2. Смотрит очередь `Chat_Queue_1`
3. Есть свободный оператор → назначить
4. Нет → ждать в очереди
5. Таймаут → ошибка `chat_timedout_waiting_for_agent`
6. Все заняты → ошибка `chat_request_rejected_by_agent`

### Шаг 4: Ожидание — бот слушает события

**Сценарий A — оператор найден:**

```
+0s     chat-event: {type: "chat_initialized"}          → "🔍 Ищем оператора..."
+15s    chat-event: {type: "chat_accepted"}              → (внутреннее)
+16s    chat-event: {type: "chat_agent_joined",          → "✅ Оператор Алина
                     agent: "Алина"}                        подключилась к чату"
```

**Сценарий B — все операторы заняты:**

```
+0s     chat-event: {type: "chat_initialized"}          → "🔍 Ищем оператора..."
+60s    channel-errors:                                 → "😔 Все операторы заняты.
        {code: "chat_request_rejected_by_agent"}           Попробуйте позже."
                                                        → вернуть к боту
```

**Сценарий C — таймаут ожидания:**

```
+0s     chat-event: {type: "chat_initialized"}          → "🔍 Ищем оператора..."
+120s   channel-errors:                                 → "⏰ Оператор не ответил.
        {code: "chat_timedout_waiting_for_agent"}          Возвращаю к боту."
                                                        → вернуть к боту
```

### Шаг 5: Обмен сообщениями

```
User в Telegram: "Какая ставка по ипотеке?"
     │
     ▼
Bot: emit("send-message", {message: "Какая ставка по ипотеке?"})
     │
     ▼
Middleware → Оператору в Cisco CCP
     │
     ▼
Оператор печатает: "От 22% годовых"
     │
     ▼
Middleware → Bot:
chat-event: {type: "chat_message", from: "agent", text: "От 22% годовых"}
     │
     ▼
Bot → Telegram: "👤 (Алина): От 22% годовых"
```

### Шаг 6: Завершение чата

**Вариант A — пользователь нажал "🤖 Назад к боту":**

```
Bot: emit("send-leave", {})
Middleware → chat-event: {type: "chat_left"}
Bot: sio.disconnect()
     set_human_mode(False) → возврат к AI-агенту
```

**Вариант B — оператор закрыл чат:**

```
Middleware → chat-event: {type: "chat_left"}
Bot: sio.disconnect()
     set_human_mode(False)
     → Telegram: "Оператор завершил чат. Возвращаю к боту."
```

**Вариант C — таймаут бездействия (5 минут, настраивается):**

```
... 5 минут без сообщений ...
Middleware → chat-event: {type: "chat_ended"}
Bot: sio.disconnect()
     set_human_mode(False)
     → Telegram: "Чат завершён из-за неактивности."
```

---

## Полный цикл (timeline)

```
Время    Telegram User              Bot (FastAPI)                      Chat Middleware (Cisco)
─────    ──────────────             ──────────────                     ──────────────────────

12:00    "👤 Оператор" ──────────►  enable_human_mode()
                                    set_human_mode(True)
                                         │
12:00                               POST /api/users/login ──────────►  JWT auth
                                    ◄── token: "eyJ..."

12:00                               sio.connect(token) ─────────────►  WebSocket connected
                                    emit("start-chat", {              start-chat
                                      csq: "Chat_Queue_1",
                                      message: "Вопрос про ипотеку",
                                      customerName: "@user"
                                    })
                                         │
12:00    "🔍 Ищем оператора..."  ◄───────┘

                                                                       Cisco UCCX:
                                                                       очередь → ищет оператора

12:00                               on("chat-event") ◄────────────── {type: "chat_initialized"}

12:15                               on("chat-event") ◄────────────── {type: "chat_accepted"}

12:16                               on("chat-event") ◄────────────── {type: "chat_agent_joined",
                                         │                             agent: "Алина"}
12:16    "✅ Алина подключилась"  ◄───────┘

12:17    "Какая ставка?" ────────►  emit("send-message",
                                      {message: "Какая ставка?"}) ──►  → оператору
                                         │
12:18                               on("chat-event") ◄────────────── {type: "chat_message",
                                         │                             from: "agent",
                                         │                             text: "От 22% годовых"}
12:18    "👤 (Алина): От 22%"  ◄─────────┘

12:25    "🤖 Назад к боту" ──────►  emit("send-leave", {}) ─────────►  send-leave
                                    sio.disconnect()
                                    set_human_mode(False)
                                         │
12:25    "Вернулись к боту.      ◄───────┘
          Продолжаем."
```

---

## Где что хранится

```
┌────────────────────────────────────────────────────────────────┐
│                       Наш Bot (FastAPI)                         │
│                                                                 │
│  В памяти (dict):                                               │
│  ┌────────────────────────────────────────────┐                 │
│  │ connections = {                             │                 │
│  │   "session-uuid-1": ChatConnection(         │                 │
│  │       sio = <socket.io client>,             │                 │
│  │       jwt_token = "eyJ...",                 │                 │
│  │       chat_active = True,                   │                 │
│  │       agent_name = "Алина",                 │                 │
│  │   ),                                        │                 │
│  │   "session-uuid-2": ChatConnection(...),    │                 │
│  │ }                                           │                 │
│  └────────────────────────────────────────────┘                 │
│                                                                 │
│  В PostgreSQL (как сейчас):                                     │
│  - ChatSession.human_mode = True/False                          │
│  - Message(role="user" / "operator" / "agent")                  │
│                                                                 │
│  LangGraph (как сейчас):                                        │
│  - interrupt() при human_mode                                   │
│  - Command(resume=...) при ответе оператора                     │
└────────────────────────────────────────────────────────────────┘

┌────────────────────────────────────────────────────────────────┐
│                Chat Middleware (Cisco UCCX)                      │
│                                                                 │
│  Управляет сам (нам не нужно кодить):                           │
│  - Очередь операторов                                           │
│  - Назначение свободного оператора                              │
│  - Таймауты ожидания оператора                                  │
│  - Таймауты бездействия (5 мин, настраивается)                  │
│  - Отказ если все операторы заняты                               │
│  - JWT авторизация каналов                                      │
└────────────────────────────────────────────────────────────────┘
```

---

## Обработка ошибок

| Событие из Middleware | Код ошибки | Реакция бота |
|----------------------|------------|--------------|
| Все операторы заняты | `chat_request_rejected_by_agent` | "Все операторы заняты. Попробуйте позже." → возврат к боту |
| Таймаут ожидания оператора | `chat_timedout_waiting_for_agent` | "Оператор не ответил. Возвращаю к боту." → возврат к боту |
| Таймаут бездействия (5 мин) | `chat_ended` (в chat-event) | "Чат завершён из-за неактивности." → возврат к боту |
| Оператор закрыл чат | `chat_left` (в chat-event) | "Оператор завершил чат." → возврат к боту |
| Socket.IO disconnect | disconnect event | Попытка reconnect (3 попытки), потом возврат к боту |
| JWT истёк (3 часа) | 401 при connect | Получить новый токен и переподключиться |
| Middleware недоступен | connection error | Fallback: алерт операторам в Telegram (старое поведение) |

---

## История сообщений для оператора

При начале чата бот передаёт контекст в поле `message` команды `start-chat`.
Формируем краткое описание из последних сообщений сессии:

```python
# Собираем контекст из DB
messages = await chat_service.get_recent_messages(session_id, limit=10)
context = "\n".join([
    f"{'User' if m.role == 'user' else 'Bot'}: {m.text}"
    for m in messages
])

sio.emit("start-chat", {
    "csq": "Chat_Queue_1",
    "message": context,              # оператор увидит историю
    "customerName": "@username",
})
```

---

## Передача файлов

Порядок обмена файлами через Chat Middleware:

1. Backend бота получает файл от пользователя через Telegram API
2. Проверяет файл (тип, размер, ограничения)
3. Сохраняет в директорию для статических файлов (видимую веб-серверу)
4. Формирует HTTP-ссылку на файл
5. Отправляет ссылку как обычное текстовое сообщение через `send-message`

```python
# Пользователь отправил фото/документ
file_url = await save_and_get_url(telegram_file)

sio.emit("send-message", {
    "message": file_url    # оператор получит как ссылку
})
```

---

## Nginx Reverse Proxy

Chat Middleware доступен только из корпоративной сети. На nginx бота настраиваем reverse proxy:

```nginx
# nginx.conf
location /ws/chat-middleware/ {
    proxy_pass https://chat-middleware.asakabank.uz/api/ws/chats/;
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_set_header Host chat-middleware.asakabank.uz;
    proxy_read_timeout 86400s;
    proxy_send_timeout 86400s;

    # DNS: chat-middleware.asakabank.uz доступен только через
    # корпоративный DNS (167.33.33.55)
    resolver 167.33.33.55;
}
```

---

## Изменения в проекте Bot

| Файл | Что меняется |
|------|-------------|
| `app/config.py` | +6 настроек (см. ниже) |
| `app/services/chat_middleware_client.py` | **НОВЫЙ** — Socket.IO клиент (~200 строк) |
| `app/api/fastapi_app.py` | В lifespan: создать `ChatMiddlewareClient` с callbacks. При shutdown: `close_all()` |
| `app/bot/handlers/commands.py` | `enable_human_mode`: вызвать `middleware.start_chat()`. `disable_human_mode`: вызвать `middleware.end_chat()` |
| `app/services/chat_service.py` | В `human_mode=True`: вызвать `middleware.send_message()` для пересылки в CRM |
| `app/bot/i18n.py` | +4 строки перевода |
| `requirements.txt` | +`python-socketio[asyncio_client]` |

### Новые настройки (`app/config.py`)

| Переменная | Обязательна | По умолчанию | Описание |
|------------|:-----------:|:------------:|----------|
| `MIDDLEWARE_ENABLED` | — | `false` | Включить интеграцию с Chat Middleware |
| `MIDDLEWARE_URL` | при enabled | — | `https://chat-middleware.asakabank.uz` |
| `MIDDLEWARE_LOGIN` | при enabled | — | Логин канала для JWT |
| `MIDDLEWARE_PASSWORD` | при enabled | — | Пароль канала для JWT |
| `MIDDLEWARE_CSQ` | при enabled | — | Contact Service Queue в Cisco UCCX |
| `MIDDLEWARE_NGINX_WS_URL` | — | — | URL через nginx reverse proxy (если нужен) |

### Новые строки перевода (`app/bot/i18n.py`)

| Ключ | ru | en | uz |
|------|----|----|-----|
| `searching_operator` | Ищем свободного оператора... | Looking for available operator... | Operator qidirilmoqda... |
| `operator_joined` | Оператор {name} подключился к чату | Operator {name} joined the chat | Operator {name} chatga qo'shildi |
| `all_operators_busy` | Все операторы заняты. Попробуйте позже | All operators are busy. Please try later | Barcha operatorlar band. Keyinroq urinib ko'ring |
| `operator_wait_timeout` | Оператор не ответил. Возвращаю к боту | Operator did not respond. Returning to bot | Operator javob bermadi. Botga qaytarilmoqda |

## Обратная совместимость

- `MIDDLEWARE_ENABLED=false` (default) → работает как сейчас через `OPERATOR_IDS` в Telegram
- `MIDDLEWARE_ENABLED=true` → новый flow через Chat Middleware
- Fallback: если Middleware недоступен → алерт операторам в Telegram (старое поведение)
- `/op` команда, `/operator/send` API, SQLAdmin operator_reply — продолжают работать

## Что НЕ нужно

- ~~REST webhook endpoint в боте~~ — middleware шлёт события через Socket.IO
- ~~Новые поля в DB (crm_chat_id)~~ — state хранится в памяти (dict connections)
- ~~Celery, Redis, Django Channels в CRM~~ — Cisco UCCX управляет этим сам
- ~~Модели Operator, Chat в CRM~~ — Cisco управляет операторами

---

## Реализация: Socket.IO клиент

### `app/services/chat_middleware_client.py`

```python
"""
Клиент для Chat Middleware System (Cisco UCCX).
Управляет Socket.IO соединениями для каждой сессии в human_mode.
"""

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional, Callable, Awaitable

import httpx
import socketio

logger = logging.getLogger(__name__)


@dataclass
class ChatConnection:
    """Одно активное Socket.IO соединение = один чат с оператором."""
    session_id: str
    sio: socketio.AsyncClient
    jwt_token: str
    chat_active: bool = False
    agent_name: Optional[str] = None
    connected_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class ChatMiddlewareClient:
    """
    Управляет подключениями к Chat Middleware System.
    Один экземпляр на приложение, держит dict[session_id -> ChatConnection].
    """

    def __init__(
        self,
        middleware_url: str,
        login: str,
        password: str,
        csq: str,
        on_agent_message: Callable[..., Awaitable],
        on_agent_joined: Callable[..., Awaitable],
        on_chat_ended: Callable[..., Awaitable],
        on_error: Callable[..., Awaitable],
        nginx_ws_url: Optional[str] = None,
    ):
        self.middleware_url = middleware_url.rstrip('/')
        self.login = login
        self.password = password
        self.csq = csq
        self.on_agent_message = on_agent_message
        self.on_agent_joined = on_agent_joined
        self.on_chat_ended = on_chat_ended
        self.on_error = on_error
        self.ws_url = nginx_ws_url or middleware_url

        self._connections: dict[str, ChatConnection] = {}
        self._http = httpx.AsyncClient(timeout=10)

    # ─── JWT ──────────────────────────────────────────────

    async def _get_jwt_token(self) -> str:
        resp = await self._http.post(
            f"{self.middleware_url}/api/users/login",
            json={"login": self.login, "password": self.password},
        )
        resp.raise_for_status()
        return resp.json()["token"]

    # ─── Начать чат ───────────────────────────────────────

    async def start_chat(
        self,
        session_id: str,
        user_name: str,
        initial_message: str,
    ) -> bool:
        if session_id in self._connections:
            return False

        try:
            token = await self._get_jwt_token()

            sio = socketio.AsyncClient(
                reconnection=True,
                reconnection_attempts=3,
                reconnection_delay=2,
            )

            conn = ChatConnection(
                session_id=session_id,
                sio=sio,
                jwt_token=token,
            )
            self._connections[session_id] = conn
            self._register_handlers(sio, session_id)

            await sio.connect(
                self.ws_url,
                socketio_path="/api/ws/chats/",
                transports=["websocket"],
                auth={"token": token},
            )

            await sio.emit("start-chat", {
                "csq": self.csq,
                "message": initial_message,
                "customerName": user_name,
            })

            logger.info("Chat started for session %s", session_id)
            return True

        except Exception as exc:
            logger.exception("Failed to start chat: %s", exc)
            self._connections.pop(session_id, None)
            return False

    # ─── Отправить сообщение ──────────────────────────────

    async def send_message(self, session_id: str, text: str) -> bool:
        conn = self._connections.get(session_id)
        if not conn or not conn.chat_active:
            return False
        try:
            await conn.sio.emit("send-message", {"message": text})
            return True
        except Exception as exc:
            logger.exception("Failed to send: %s", exc)
            return False

    # ─── Завершить чат ────────────────────────────────────

    async def end_chat(self, session_id: str) -> None:
        conn = self._connections.pop(session_id, None)
        if conn is None:
            return
        try:
            if conn.chat_active:
                await conn.sio.emit("send-leave", {})
                await asyncio.sleep(0.5)
            await conn.sio.disconnect()
        except Exception as exc:
            logger.warning("Error ending chat: %s", exc)

    # ─── Обработчики Socket.IO ────────────────────────────

    def _register_handlers(self, sio: socketio.AsyncClient, session_id: str):

        @sio.on("chat-event")
        async def on_chat_event(data):
            event_type = data.get("type", "")

            if event_type == "chat_initialized":
                conn = self._connections.get(session_id)
                if conn:
                    conn.chat_active = True

            elif event_type == "chat_agent_joined":
                agent_name = data.get("agent", "Оператор")
                conn = self._connections.get(session_id)
                if conn:
                    conn.agent_name = agent_name
                await self.on_agent_joined(session_id, agent_name)

            elif event_type == "chat_message":
                if data.get("from") == "agent":
                    await self.on_agent_message(session_id, data.get("text", ""))

            elif event_type in ("chat_left", "chat_closed", "chat_ended"):
                reason = data.get("reason", event_type)
                await self.on_chat_ended(session_id, reason)
                await self._cleanup(session_id)

        @sio.on("channel-errors")
        async def on_error(data):
            code = data.get("code", "unknown")
            await self.on_error(session_id, code)
            await self._cleanup(session_id)

        @sio.on("disconnect")
        async def on_disconnect():
            logger.info("Disconnected for session %s", session_id)

    async def _cleanup(self, session_id: str):
        conn = self._connections.pop(session_id, None)
        if conn:
            conn.chat_active = False
            try:
                await conn.sio.disconnect()
            except Exception:
                pass

    # ─── Lifecycle ────────────────────────────────────────

    async def close_all(self):
        for session_id in list(self._connections.keys()):
            await self.end_chat(session_id)
        await self._http.aclose()

    def has_active_chat(self, session_id: str) -> bool:
        conn = self._connections.get(session_id)
        return conn is not None and conn.chat_active

    def get_agent_name(self, session_id: str) -> Optional[str]:
        conn = self._connections.get(session_id)
        return conn.agent_name if conn else None
```

---

## Интеграция в бот: callbacks

При инициализации `ChatMiddlewareClient` в `fastapi_app.py` передаём 4 callback'а. Каждый вызывается автоматически при событии из middleware:

### `on_agent_joined(session_id, agent_name)`

Оператор подключился к чату.

```python
async def on_agent_joined(session_id: str, agent_name: str):
    session_data = await chat_service.get_session_with_user(session_id)
    if not session_data:
        return
    chat_session, user = session_data
    lang = normalize_lang(user.language)

    await bot.send_message(
        chat_id=user.telegram_user_id,
        text=t("operator_joined", lang, name=agent_name),
    )
```

### `on_agent_message(session_id, text)`

Оператор отправил сообщение.

```python
async def on_agent_message(session_id: str, text: str):
    session_data = await chat_service.get_session_with_user(session_id)
    if not session_data:
        return
    chat_session, user = session_data

    # Сохранить в БД
    await chat_service._save_message(session_id, role="operator", text=text)

    # Отправить в Telegram
    agent_name = middleware_client.get_agent_name(session_id) or "Оператор"
    formatted = f"👤 ({agent_name}): {text}"
    await bot.send_message(chat_id=user.telegram_user_id, text=formatted)

    # Resume LangGraph
    try:
        await agent_client.resume_human_mode(session_id, text)
    except Exception:
        pass
```

### `on_chat_ended(session_id, reason)`

Чат завершён (оператором, таймаутом, или системой).

```python
async def on_chat_ended(session_id: str, reason: str):
    session_data = await chat_service.get_session_with_user(session_id)
    if not session_data:
        return
    chat_session, user = session_data
    lang = normalize_lang(user.language)

    await chat_service.set_human_mode(session_id, False)

    if reason == "timeout":
        text = t("operator_wait_timeout", lang)
    else:
        text = t("human_timeout_back_to_bot", lang, minutes=0)

    await bot.send_message(chat_id=user.telegram_user_id, text=text)
```

### `on_error(session_id, error_code)`

Ошибка: все операторы заняты или таймаут ожидания.

```python
async def on_error(session_id: str, error_code: str):
    session_data = await chat_service.get_session_with_user(session_id)
    if not session_data:
        return
    chat_session, user = session_data
    lang = normalize_lang(user.language)

    await chat_service.set_human_mode(session_id, False)

    if error_code == "chat_request_rejected_by_agent":
        text = t("all_operators_busy", lang)
    elif error_code == "chat_timedout_waiting_for_agent":
        text = t("operator_wait_timeout", lang)
    else:
        text = t("all_operators_busy", lang)

    await bot.send_message(chat_id=user.telegram_user_id, text=text)
```

---

## Порядок реализации

1. `pip install python-socketio[asyncio_client]` → `requirements.txt`
2. `app/config.py` — добавить `MIDDLEWARE_*` настройки
3. `app/services/chat_middleware_client.py` — Socket.IO клиент (код выше)
4. `app/api/fastapi_app.py` — создать client в lifespan, передать callbacks
5. `app/bot/handlers/commands.py` — `enable_human_mode` → `middleware.start_chat()`, `disable_human_mode` → `middleware.end_chat()`
6. `app/services/chat_service.py` — `handle_user_message` при `human_mode=True` → `middleware.send_message()`
7. `app/bot/i18n.py` — новые строки перевода
8. `nginx.conf` — reverse proxy к middleware (если нужен)

## Верификация

1. Проверить JWT авторизацию: `POST /api/users/login`
2. Проверить Socket.IO подключение через nginx reverse proxy
3. Тест: начать чат → дождаться оператора → обменяться сообщениями → закрыть
4. Тест ошибок: все операторы offline → `chat_request_rejected_by_agent`
5. Тест таймаута: не отвечать 5 мин → `chat_ended`
6. Тест fallback: middleware недоступен → алерт через Telegram (старое поведение)
