# Интеграция Bot ↔ CRM: Очередь операторов

Архитектура интеграции Telegram-бота с внешней CRM-системой (Django) для автоматической маршрутизации чатов к свободным операторам с очередью ожидания.

---

## Текущее состояние

Сейчас при нажатии "👤 Живой оператор" бот рассылает уведомление **всем** операторам из `OPERATOR_IDS` в Telegram. Нет очереди, нет автоматического назначения, нет контроля загрузки.

## Целевое состояние

При переходе в режим оператора бот отправляет запрос в CRM, CRM ставит чат в очередь и назначает первому свободному оператору. Оператор общается через веб-чат в CRM. Все сообщения проходят через API.

---

## Общая схема

```
┌──────────────┐         ┌──────────────────┐         ┌──────────────────────────────────────────┐
│   Telegram   │ ──────► │   Bot (FastAPI)   │ ──────► │              CRM (Django)                 │
│    User      │ ◄────── │                    │ ◄────── │                                          │
└──────────────┘         └──────────────────┘         │  ┌─────────┐  ┌───────┐  ┌────────────┐  │
                                                       │  │ Celery  │  │ Redis │  │  Channels  │  │
                                                       │  │ Worker  │  │       │  │ (WebSocket)│  │
                                                       │  └────┬────┘  └───┬───┘  └─────┬──────┘  │
                                                       │       │           │             │         │
                                                       │       ▼           ▼             ▼         │
                                                       │  ┌──────────┐              ┌──────────┐  │
                                                       │  │PostgreSQL│              │ Браузер  │  │
                                                       │  │          │              │ оператора│  │
                                                       │  └──────────┘              └──────────┘  │
                                                       └──────────────────────────────────────────┘
```

### 5 потоков данных

| # | Событие | Направление | Endpoint |
|---|---------|-------------|----------|
| 1 | User нажимает "Оператор" | Bot → CRM | `POST /api/chats/` |
| 2 | CRM назначает оператора / обновляет очередь | CRM → Bot | `POST /crm/webhook` |
| 3 | User пишет сообщение | Bot → CRM | `POST /api/chats/{id}/messages/` |
| 4 | Оператор отвечает | CRM → Bot | `POST /operator/send` (уже есть) |
| 5 | Сессия закрывается | Bot → CRM | `POST /api/chats/{id}/close/` |

---

## Стек CRM

| Компонент | Роль |
|-----------|------|
| **Django REST API** | Приём запросов от бота, REST для фронтенда оператора |
| **Celery + Redis (broker)** | Фоновые задачи: назначение оператора, обработка очереди, webhook'и |
| **Redis (pub/sub)** | Real-time уведомления операторам через WebSocket |
| **Redis (distributed lock)** | Защита от race condition при назначении (два worker'а не схватят один чат) |
| **Django Channels + WebSocket** | Постоянное соединение с браузером оператора |
| **PostgreSQL** | Source of truth — все данные, состояние очереди |

---

## Компонент 1: PostgreSQL — источник истины

Единственное место где хранится состояние. Если Redis упал — данные не потеряны.

### Модели

```python
# ═══════════════════════════════════════════════
# Оператор
# ═══════════════════════════════════════════════
class Operator(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE)
    name = models.CharField(max_length=255)
    is_online = models.BooleanField(default=False)       # оператор залогинен
    max_concurrent_chats = models.IntegerField(default=3) # макс одновременных чатов
    last_seen_at = models.DateTimeField(auto_now=True)

    def get_active_chats_count(self):
        return self.chats.filter(status=Chat.Status.ASSIGNED).count()

    def is_available(self):
        return self.is_online and self.get_active_chats_count() < self.max_concurrent_chats


# ═══════════════════════════════════════════════
# Чат (= одна заявка в очереди)
# ═══════════════════════════════════════════════
class Chat(models.Model):
    class Status(models.TextChoices):
        QUEUED   = 'queued'       # в очереди, ждёт оператора
        ASSIGNED = 'assigned'     # назначен оператору
        CLOSED   = 'closed'       # завершён

    # Связь с ботом
    external_session_id = models.CharField(max_length=36, unique=True, db_index=True)

    # Оператор
    operator = models.ForeignKey(Operator, null=True, blank=True,
                                  related_name='chats', on_delete=models.SET_NULL)
    status = models.CharField(max_length=16, choices=Status.choices,
                               default=Status.QUEUED)
    queue_position = models.IntegerField(default=0)
    priority = models.IntegerField(default=0)              # 0=обычный, 1+=VIP

    # Контекст от бота
    user_name = models.CharField(max_length=255)
    user_phone = models.CharField(max_length=32, null=True, blank=True)
    channel = models.CharField(max_length=32, default='telegram')
    category = models.CharField(max_length=64, null=True, blank=True)
    context_summary = models.TextField(null=True, blank=True)
    initial_history = models.JSONField(default=list)        # история с ботом

    # Время
    created_at = models.DateTimeField(auto_now_add=True)
    assigned_at = models.DateTimeField(null=True, blank=True)
    closed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-priority', 'created_at']  # VIP первые, затем FIFO


# ═══════════════════════════════════════════════
# Сообщения внутри чата
# ═══════════════════════════════════════════════
class ChatMessage(models.Model):
    class Sender(models.TextChoices):
        USER     = 'user'
        OPERATOR = 'operator'

    chat = models.ForeignKey(Chat, related_name='messages', on_delete=models.CASCADE)
    sender = models.CharField(max_length=16, choices=Sender.choices)
    text = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)


# ═══════════════════════════════════════════════
# Лог webhook'ов (для дебага)
# ═══════════════════════════════════════════════
class WebhookLog(models.Model):
    chat = models.ForeignKey(Chat, on_delete=models.CASCADE)
    event = models.CharField(max_length=32)
    payload = models.JSONField()
    status_code = models.IntegerField(null=True)
    sent_at = models.DateTimeField(auto_now_add=True)
```

---

## Компонент 2: Django REST API — приём запросов от бота

Точка входа. Бот шлёт HTTP запросы сюда.

### Endpoint 1: `POST /api/chats/` — создать чат

```python
class ChatCreateView(APIView):
    """
    Бот вызывает когда пользователь нажал "Оператор".
    Создаёт Chat в БД и кидает задачу в Celery на назначение.
    """
    permission_classes = [BotAPIKeyPermission]

    def post(self, request):
        data = request.data

        # 1. Сохраняем в PostgreSQL
        chat = Chat.objects.create(
            external_session_id=data['external_session_id'],
            user_name=data.get('user_name', ''),
            user_phone=data.get('user_phone'),
            channel=data.get('channel', 'telegram'),
            category=data.get('category'),
            context_summary=data.get('context_summary'),
            initial_history=data.get('messages_history', []),
            status=Chat.Status.QUEUED,
        )

        # 2. Считаем позицию в очереди
        position = Chat.objects.filter(
            status=Chat.Status.QUEUED,
            created_at__lte=chat.created_at
        ).count()
        chat.queue_position = position
        chat.save(update_fields=['queue_position'])

        # 3. Кидаем задачу в Redis → Celery подхватит
        #    НЕ ждём результата — отвечаем боту сразу
        from .tasks import try_assign_chat
        try_assign_chat.delay(chat.id)

        # 4. Мгновенный ответ боту
        return Response({
            'chat_id': str(chat.id),
            'status': 'queued',
            'queue_position': position,
            'operator': None,
        }, status=status.HTTP_201_CREATED)
```

**Почему не назначаем оператора прямо здесь?**
Назначение включает: lock Redis, `select_for_update` в БД, webhook в бот, pub/sub оператору — ~200мс. Лучше ответить "queued" за 20мс, а назначение сделать в фоне через Celery.

### Endpoint 2: `POST /api/chats/{id}/messages/` — сообщение от пользователя

```python
class ChatMessageView(APIView):
    """Бот пересылает сообщение пользователя в CRM."""
    permission_classes = [BotAPIKeyPermission]

    def post(self, request, chat_id):
        chat = get_object_or_404(Chat, id=chat_id, status=Chat.Status.ASSIGNED)

        # 1. Сохраняем в PostgreSQL
        msg = ChatMessage.objects.create(
            chat=chat,
            sender=ChatMessage.Sender.USER,
            text=request.data['text'],
        )

        # 2. Push оператору через Redis pub/sub → WebSocket
        notify_operator(chat.operator_id, {
            'type': 'new_message',
            'chat_id': str(chat.id),
            'sender': 'user',
            'text': msg.text,
            'timestamp': msg.created_at.isoformat(),
        })

        return Response({'ok': True})
```

### Endpoint 3: `POST /api/chats/{id}/close/` — бот закрывает чат

```python
class ChatCloseView(APIView):
    """Бот сообщает что сессия закрыта (timeout, user ушёл)."""
    permission_classes = [BotAPIKeyPermission]

    def post(self, request, chat_id):
        chat = get_object_or_404(Chat, id=chat_id)

        was_queued = chat.status == Chat.Status.QUEUED
        was_assigned = chat.status == Chat.Status.ASSIGNED

        chat.status = Chat.Status.CLOSED
        chat.closed_at = now()
        chat.save()

        if was_assigned and chat.operator_id:
            notify_operator(chat.operator_id, {
                'type': 'chat_closed',
                'chat_id': str(chat.id),
                'reason': request.data.get('reason', 'user_ended'),
            })

        # Освободилось место → обработать очередь
        if was_queued or was_assigned:
            from .tasks import process_queue
            process_queue.delay()

        return Response({'ok': True})
```

### Endpoint 4: Оператор отправил сообщение (из браузера)

```python
class OperatorReplyView(APIView):
    """Оператор пишет ответ в веб-чате CRM."""
    permission_classes = [IsAuthenticated, IsOperator]

    def post(self, request, chat_id):
        chat = get_object_or_404(Chat, id=chat_id, status=Chat.Status.ASSIGNED)
        operator = request.user.operator
        text = request.data['text']

        # 1. Сохраняем в PostgreSQL
        ChatMessage.objects.create(
            chat=chat,
            sender=ChatMessage.Sender.OPERATOR,
            text=text,
        )

        # 2. Отправляем в бот → бот отправит в Telegram
        from .tasks import send_to_bot
        send_to_bot.delay(
            session_id=chat.external_session_id,
            text=text,
            operator_name=operator.name,
            operator_id=operator.id,
        )

        return Response({'ok': True})
```

---

## Компонент 3: Celery + Redis (broker) — фоновые задачи

Выполняет тяжёлую работу в фоне, не блокируя API.

```
Django API                    Redis (broker)              Celery Worker
    │                              │                           │
    │  try_assign_chat.delay(42)   │                           │
    │─────────────────────────────►│  queue: [task:assign:42]  │
    │                              │──────────────────────────►│
    │  ← 200 OK боту (20ms)       │                           │ выполняет
    │                              │                           │ try_assign_chat(42)
    │                              │                           │ ~200ms
```

### Задача 1: Попробовать назначить конкретный чат

```python
@shared_task(bind=True, max_retries=3, default_retry_delay=2)
def try_assign_chat(self, chat_id):
    """
    Вызывается при создании нового чата.
    Пытается найти свободного оператора и назначить.
    """
    lock = redis_client.lock("queue_lock", timeout=15)

    if not lock.acquire(blocking=True, blocking_timeout=5):
        self.retry()
        return

    try:
        with transaction.atomic():
            chat = Chat.objects.select_for_update().get(id=chat_id)

            if chat.status != Chat.Status.QUEUED:
                return  # уже назначен/закрыт

            operator = _find_best_operator()

            if operator:
                _assign_chat_to_operator(chat, operator)
            else:
                _update_queue_position(chat)
    finally:
        lock.release()
```

### Задача 2: Обработать всю очередь

```python
@shared_task(bind=True, max_retries=2, default_retry_delay=3)
def process_queue(self):
    """
    Вызывается когда оператор освобождается:
    - закрыл чат
    - залогинился
    - увеличили max_concurrent_chats
    """
    lock = redis_client.lock("queue_lock", timeout=30)

    if not lock.acquire(blocking=True, blocking_timeout=5):
        self.retry()
        return

    try:
        with transaction.atomic():
            queued_chats = list(
                Chat.objects
                .filter(status=Chat.Status.QUEUED)
                .select_for_update()
                .order_by('-priority', 'created_at')
            )

            for chat in queued_chats:
                operator = _find_best_operator()
                if not operator:
                    break
                _assign_chat_to_operator(chat, operator)

            _recalculate_queue_positions()
    finally:
        lock.release()
```

### Задача 3: Отправить сообщение оператора в бот

```python
@shared_task(bind=True, max_retries=3, default_retry_delay=5)
def send_to_bot(self, session_id, text, operator_name, operator_id):
    """CRM → POST bot /operator/send → бот отправит в Telegram."""
    try:
        resp = requests.post(
            f"{settings.BOT_API_URL}/operator/send",
            json={
                'session_id': session_id,
                'text': text,
                'operator_name': operator_name,
                'operator_id': operator_id,
            },
            headers={'X-API-Key': settings.BOT_API_KEY},
            timeout=10,
        )
        resp.raise_for_status()
    except requests.RequestException as exc:
        self.retry(exc=exc)
```

### Задача 4: Webhook в бот (уведомление о статусе)

```python
@shared_task(bind=True, max_retries=3, default_retry_delay=5)
def send_bot_webhook(self, chat_id, event, data):
    """Отправляет webhook в бот при изменении статуса чата."""
    chat = Chat.objects.get(id=chat_id)
    payload = {
        'event': event,
        'chat_id': str(chat.id),
        'external_session_id': chat.external_session_id,
        'data': data,
    }
    try:
        resp = requests.post(
            f"{settings.BOT_WEBHOOK_URL}/crm/webhook",
            json=payload,
            headers={'X-Webhook-Secret': settings.BOT_WEBHOOK_SECRET},
            timeout=10,
        )
        resp.raise_for_status()
        status_code = resp.status_code
    except requests.RequestException as exc:
        WebhookLog.objects.create(
            chat=chat, event=event, payload=payload, status_code=0
        )
        self.retry(exc=exc)
        return

    WebhookLog.objects.create(
        chat=chat, event=event, payload=payload, status_code=status_code
    )
```

### Вспомогательные функции

```python
def _find_best_operator():
    """Найти наименее загруженного свободного оператора."""
    from django.db.models import Count, Q, F

    return (
        Operator.objects
        .filter(is_online=True)
        .annotate(
            active_chats=Count(
                'chats',
                filter=Q(chats__status=Chat.Status.ASSIGNED)
            )
        )
        .filter(active_chats__lt=F('max_concurrent_chats'))
        .order_by('active_chats')  # наименее загруженный первый
        .first()
    )


def _assign_chat_to_operator(chat, operator):
    """Назначить чат оператору + уведомления."""
    chat.operator = operator
    chat.status = Chat.Status.ASSIGNED
    chat.assigned_at = now()
    chat.queue_position = 0
    chat.save()

    # Webhook → бот → Telegram: "Оператор подключился"
    send_bot_webhook.delay(
        chat_id=chat.id,
        event='chat.assigned',
        data={'operator': {'id': operator.id, 'name': operator.name}}
    )

    # Redis pub/sub → WebSocket → браузер оператора: "Новый чат"
    notify_operator(operator.id, {
        'type': 'new_chat',
        'chat_id': str(chat.id),
        'user_name': chat.user_name,
        'category': chat.category,
        'context_summary': chat.context_summary,
        'history': chat.initial_history,
    })


def _update_queue_position(chat):
    """Обновить позицию и уведомить бот."""
    position = Chat.objects.filter(
        status=Chat.Status.QUEUED,
        created_at__lte=chat.created_at
    ).count()
    chat.queue_position = position
    chat.save(update_fields=['queue_position'])

    send_bot_webhook.delay(
        chat_id=chat.id,
        event='chat.queued',
        data={'queue_position': position}
    )


def _recalculate_queue_positions():
    """Пересчитать позиции всех в очереди после назначения."""
    remaining = (
        Chat.objects
        .filter(status=Chat.Status.QUEUED)
        .order_by('-priority', 'created_at')
    )
    for pos, chat in enumerate(remaining, start=1):
        if chat.queue_position != pos:
            chat.queue_position = pos
            chat.save(update_fields=['queue_position'])
            send_bot_webhook.delay(
                chat_id=chat.id,
                event='chat.queued',
                data={'queue_position': pos}
            )
```

### Триггеры вызова process_queue

```python
# 1. Оператор закрыл чат
def close_chat(chat):
    chat.status = 'closed'
    chat.closed_at = now()
    chat.save()
    process_queue.delay()           # ← в Redis → Celery подхватит мгновенно

# 2. Оператор зашёл в систему
def operator_login(operator):
    operator.is_online = True
    operator.save()
    process_queue.delay()           # ← может взять чаты из очереди

# 3. Оператор вышел — переназначить его чаты
def operator_logout(operator):
    operator.is_online = False
    operator.save()
    reassign_operator_chats.delay(operator.id)
```

### Зачем Celery

```
Без Celery:                              С Celery:
───────────                              ─────────

POST /api/chats/                         POST /api/chats/
  │                                        │
  ├─ create Chat          20ms             ├─ create Chat           20ms
  ├─ select_for_update    30ms             ├─ try_assign.delay()    2ms ← кинули в Redis
  ├─ find operator        20ms             └─ return 200            ← 22ms !!!
  ├─ save assignment      15ms
  ├─ webhook → bot        100ms            А в фоне Celery worker:
  ├─ redis publish        5ms              ├─ select_for_update    30ms
  └─ return 200           ← 190ms          ├─ find operator        20ms
                                           ├─ save assignment      15ms
                                           ├─ webhook → bot        100ms
                                           └─ redis publish        5ms
```

Бот получает ответ за **22ms** вместо **190ms**.

---

## Компонент 4: Redis distributed lock — защита от race condition

**Проблема:** Два Celery worker'а одновременно запускают `process_queue()`:

```
Worker A: "Алина свободна, назначу ей Chat 5"
Worker B: "Алина свободна, назначу ей Chat 6"
                    │
                    ▼
         Алина получила 2 чата, хотя max = 1 !!!
```

**Решение: Redis lock + PostgreSQL `select_for_update()`**

```
Worker A: lock.acquire() ✅ → работает с очередью
Worker B: lock.acquire() ❌ → ждёт 5 сек → retry
                    │
                    ▼
Worker A: назначил Chat 5 Алине → lock.release()
Worker B: lock.acquire() ✅ → Алина занята → назначает Chat 6 Максиму
```

Двойная защита:

```python
# 1. Redis lock — только один worker обрабатывает очередь
lock = redis_client.lock("queue_lock", timeout=15)
lock.acquire(blocking=True, blocking_timeout=5)

# 2. PostgreSQL select_for_update — строки Chat заблокированы на уровне БД
Chat.objects.filter(status='queued').select_for_update()
```

Вместе гарантируют: один worker в один момент, даже если lock "протёк" (timeout) — БД не даст прочитать грязные данные.

---

## Компонент 5: Redis Pub/Sub — real-time уведомления

Мгновенная доставка событий оператору в браузер.

```
Celery worker                Redis                     Django Channels          Браузер
     │                         │                            │                      │
     │  PUBLISH                │                            │                      │
     │  operator:5             │                            │                      │
     │  {"type":"new_chat"}    │                            │                      │
     │────────────────────────►│  channel: operator:5       │                      │
     │                         │───────────────────────────►│  WebSocket send      │
     │                         │                            │─────────────────────►│
     │                         │                            │                      │  🔔 Новый чат!
```

```python
# services.py
import json
from redis import Redis

redis_client = Redis.from_url(settings.REDIS_URL)

def notify_operator(operator_id: int, data: dict):
    """
    Публикует событие в Redis канал оператора.
    Django Channels consumer подписан на этот канал
    и пробрасывает в WebSocket.
    """
    channel = f"operator:{operator_id}"
    redis_client.publish(channel, json.dumps(data, ensure_ascii=False))
```

### Какие события получает оператор

| Событие | Когда | Данные |
|---------|-------|--------|
| `new_chat` | Назначен новый чат | chat_id, user_name, category, history |
| `new_message` | Пользователь написал | chat_id, text, timestamp |
| `chat_closed` | Пользователь ушёл / timeout | chat_id, reason |
| `queue_update` | Изменилась очередь | queue_size |

---

## Компонент 6: Django Channels + WebSocket — браузер оператора

Держит постоянное соединение с браузером. Получает события из Redis Pub/Sub и пробрасывает оператору.

```python
# consumers.py
import asyncio
import json
from channels.generic.websocket import AsyncWebsocketConsumer
from channels.db import database_sync_to_async
import redis.asyncio as aioredis


class OperatorConsumer(AsyncWebsocketConsumer):
    """
    WebSocket соединение с браузером оператора.
    ws://crm.example.com/ws/operator/
    """

    async def connect(self):
        user = self.scope.get('user')
        if not user or not user.is_authenticated:
            await self.close()
            return

        self.operator_id = await self._get_operator_id(user)
        if not self.operator_id:
            await self.close()
            return

        await self.accept()

        # Подписаться на Redis канал этого оператора
        self.redis = await aioredis.from_url(settings.REDIS_URL)
        self.pubsub = self.redis.pubsub()
        await self.pubsub.subscribe(f"operator:{self.operator_id}")

        # Фоновая задача: слушать Redis и пробрасывать в WebSocket
        self.listener = asyncio.create_task(self._redis_listener())

        # Отметить оператора как online
        await self._set_online(True)

    async def disconnect(self, code):
        if hasattr(self, 'pubsub'):
            await self.pubsub.unsubscribe(f"operator:{self.operator_id}")
            await self.pubsub.close()
        if hasattr(self, 'listener'):
            self.listener.cancel()
        if hasattr(self, 'redis'):
            await self.redis.close()
        await self._set_online(False)

    async def _redis_listener(self):
        """Слушает Redis pub/sub и отправляет в WebSocket."""
        try:
            async for message in self.pubsub.listen():
                if message['type'] == 'message':
                    data = message['data']
                    if isinstance(data, bytes):
                        data = data.decode('utf-8')
                    await self.send(text_data=data)
        except asyncio.CancelledError:
            pass

    async def receive(self, text_data=None, bytes_data=None):
        """Оператор прислал сообщение через WebSocket."""
        data = json.loads(text_data)

        if data.get('type') == 'operator_message':
            await self._handle_operator_message(
                chat_id=data['chat_id'],
                text=data['text'],
            )
        elif data.get('type') == 'close_chat':
            await self._handle_close_chat(chat_id=data['chat_id'])

    @database_sync_to_async
    def _get_operator_id(self, user):
        op = getattr(user, 'operator', None)
        return op.id if op else None

    @database_sync_to_async
    def _set_online(self, is_online):
        Operator.objects.filter(id=self.operator_id).update(is_online=is_online)
        if is_online:
            from .tasks import process_queue
            process_queue.delay()

    @database_sync_to_async
    def _handle_operator_message(self, chat_id, text):
        chat = Chat.objects.get(id=chat_id, operator_id=self.operator_id)
        ChatMessage.objects.create(chat=chat, sender='operator', text=text)
        send_to_bot.delay(
            session_id=chat.external_session_id,
            text=text,
            operator_name=chat.operator.name,
            operator_id=chat.operator.id,
        )

    @database_sync_to_async
    def _handle_close_chat(self, chat_id):
        chat = Chat.objects.get(id=chat_id, operator_id=self.operator_id)
        chat.status = Chat.Status.CLOSED
        chat.closed_at = now()
        chat.save()
        send_bot_webhook.delay(chat.id, 'chat.closed', {'reason': 'operator_closed'})
        process_queue.delay()
```

### Routing

```python
# routing.py
from django.urls import re_path
from . import consumers

websocket_urlpatterns = [
    re_path(r'ws/operator/$', consumers.OperatorConsumer.as_asgi()),
]
```

---

## Полный цикл одного чата

```
 Время    Telegram User         Bot (FastAPI)              CRM (Django)                  Оператор (браузер)
──────    ──────────────        ──────────────             ────────────                  ──────────────────

12:00    "👤 Оператор" ──────► enable_human_mode()
                                     │
12:00                          POST /api/chats/ ────────► ChatCreateView.post()
                                     │                         │
                                     │                    Chat.create(queued)            [PostgreSQL]
                                     │                    try_assign_chat.delay(42)      [→ Redis]
                               ◄── 201 {queued, pos:3}
                                     │
12:00    "⏳ Позиция: 3" ◄───────────┘
                                                          Celery worker:
                                                            lock → select_for_update
                                                            все заняты → queue_pos=3
                                                            send_bot_webhook(queued)
                                                               │
                               POST /crm/webhook ◄─────────────┘
                               event=chat.queued, pos=3

         ... проходит 5 минут, оператор Алина закрыла другой чат ...

12:05                                                    close_chat → process_queue.delay()
                                                          Celery:
                                                            lock → Алина свободна!
                                                            Chat 42 → assigned → Алина
                                                               │
                                                               ├─ send_bot_webhook(assigned)
12:05                              POST /crm/webhook ◄─────────┘
                               event=chat.assigned
                               operator=Алина
                                     │
12:05    "✅ Алина подключилась" ◄────┘
                                                               │
                                                               └─ redis.publish(operator:5)
                                                                        │
                                                                        └──────► 🔔 Новый чат!
                                                                                  История: [...]

12:06    "Какая ставка?" ────► POST /api/chats/42/msg ──► ChatMessage.create()
                                                           redis.publish(new_message)
                                                                        │
                                                                        └──────► 💬 "Какая ставка?"

12:07                                                    OperatorConsumer.receive()  ◄── "От 22%"
                                                          ChatMessage.create()
                                                          send_to_bot.delay()
                                                               │
                               POST /operator/send ◄───────────┘
                                     │
12:07    "👤 (Алина): От 22%" ◄──────┘

12:10                                                    close_chat()
                                                          process_queue.delay()
                                                          send_bot_webhook(closed)
                                                               │
                               POST /crm/webhook ◄────────────┘
                                     │
12:10    "Оператор завершил чат" ◄────┘
         set_human_mode(False)
         → возврат к боту
```

---

## Сценарий: 10 клиентов, 2 оператора (max_concurrent_chats=2)

### Заполнение очереди

```
12:00   Client 1  → Алина свободна     → ASSIGNED (Алина: 1/2)
12:01   Client 2  → Максим свободен    → ASSIGNED (Максим: 1/2)
12:02   Client 3  → Алина 1/2          → ASSIGNED (Алина: 2/2)
12:03   Client 4  → Максим 1/2         → ASSIGNED (Максим: 2/2)
12:04   Client 5  → все заняты         → QUEUED позиция 1
12:05   Client 6  → все заняты         → QUEUED позиция 2
12:06   Client 7  → все заняты         → QUEUED позиция 3
12:07   Client 8  → все заняты         → QUEUED позиция 4
12:08   Client 9  → все заняты         → QUEUED позиция 5
12:09   Client 10 → все заняты         → QUEUED позиция 6
```

### Состояние системы

```
┌─────────────────────────────────────────────────┐
│  Операторы                                       │
│  Алина  [██████████] 2/2 → Client 1, Client 3   │
│  Максим [██████████] 2/2 → Client 2, Client 4   │
├─────────────────────────────────────────────────┤
│  Очередь (FIFO)                                  │
│  1. Client 5   ⏳ "Позиция: 1"                  │
│  2. Client 6   ⏳ "Позиция: 2"                  │
│  3. Client 7   ⏳ "Позиция: 3"                  │
│  4. Client 8   ⏳ "Позиция: 4"                  │
│  5. Client 9   ⏳ "Позиция: 5"                  │
│  6. Client 10  ⏳ "Позиция: 6"                  │
└─────────────────────────────────────────────────┘
```

### Алина закрыла чат Client 1 (12:15)

```
process_queue():
  Алина: 1/2 → свободна
  Client 5 → ASSIGNED (Алина)
  Пересчёт позиций:
    Client 6:  2 → 1  → webhook "Позиция: 1"
    Client 7:  3 → 2  → webhook "Позиция: 2"
    Client 8:  4 → 3  → webhook "Позиция: 3"
    Client 9:  5 → 4  → webhook "Позиция: 4"
    Client 10: 6 → 5  → webhook "Позиция: 5"
```

### Client 7 нажал "🤖 Назад" пока в очереди

```
Bot → POST /api/chats/{id}/close/ reason=user_ended
CRM: удаляет из очереди, пересчёт:
  Client 8:  3 → 2  → webhook "Позиция: 2"
  Client 9:  4 → 3  → webhook "Позиция: 3"
  Client 10: 5 → 4  → webhook "Позиция: 4"
Client 7 возвращается к боту.
```

---

## История сообщений для оператора

При создании чата бот передаёт **всю историю текущей сессии** в поле `messages_history`:

```json
{
  "messages_history": [
    {"role": "user",  "text": "Привет",                    "ts": "12:00:01"},
    {"role": "agent", "text": "Здравствуйте! Чем помочь?", "ts": "12:00:02"},
    {"role": "user",  "text": "Хочу ипотеку",              "ts": "12:00:05"},
    {"role": "agent", "text": "Вот наши продукты: ...",     "ts": "12:00:06"},
    {"role": "user",  "text": "Хочу поговорить с человеком","ts": "12:00:15"}
  ]
}
```

CRM сохраняет в `Chat.initial_history` (JSONField) и отображает оператору:

```
┌─────────────────────────────────────────────┐
│  Чат #1234 | @username | Категория: ипотека │
├─────────────────────────────────────────────┤
│  🤖 Бот (до подключения оператора):         │
│  👤 User: Привет                            │
│  🤖 Agent: Здравствуйте! Чем помочь?        │
│  👤 User: Хочу ипотеку                      │
│  🤖 Agent: Вот наши продукты: ...           │
│  👤 User: Хочу поговорить с человеком       │
│  ─────── оператор подключился ───────       │
│  👨‍💼 Оператор: Здравствуйте, чем могу помочь?│
└─────────────────────────────────────────────┘
```

После подключения все новые сообщения приходят через `POST /api/chats/{id}/messages/` в реальном времени.

---

## API-контракт: Bot → CRM

### Аутентификация

Все запросы от бота содержат заголовок:
```
Authorization: Bearer <CRM_API_KEY>
```

### `POST /api/chats/`

```json
// Request
{
  "external_session_id": "uuid-сессии-бота",
  "user_name": "@username или ФИО",
  "user_phone": "+998901234567",
  "channel": "telegram",
  "category": "mortgage",
  "context_summary": "Пользователь интересовался ипотекой, спрашивал про ставки",
  "messages_history": [...]
}

// Response 201
{
  "chat_id": "crm-chat-uuid",
  "status": "queued",
  "queue_position": 3,
  "operator": null
}
```

### `POST /api/chats/{chat_id}/messages/`

```json
// Request
{
  "external_session_id": "uuid-сессии-бота",
  "text": "текст сообщения",
  "timestamp": "2026-03-19T12:00:00Z"
}

// Response 200
{"ok": true}
```

### `POST /api/chats/{chat_id}/close/`

```json
// Request
{
  "external_session_id": "uuid-сессии-бота",
  "reason": "user_ended"  // user_ended | timeout | bot_returned
}

// Response 200
{"ok": true}
```

---

## API-контракт: CRM → Bot

### Webhook `POST /crm/webhook`

Аутентификация: `X-Webhook-Secret` header.

```json
// chat.assigned — оператор назначен
{
  "event": "chat.assigned",
  "chat_id": "crm-chat-uuid",
  "external_session_id": "uuid-сессии-бота",
  "data": {
    "operator": {"id": 1, "name": "Алина"}
  }
}

// chat.queued — обновление позиции
{
  "event": "chat.queued",
  "chat_id": "crm-chat-uuid",
  "external_session_id": "uuid-сессии-бота",
  "data": {
    "queue_position": 2
  }
}

// chat.closed — CRM закрыла чат
{
  "event": "chat.closed",
  "chat_id": "crm-chat-uuid",
  "external_session_id": "uuid-сессии-бота",
  "data": {
    "reason": "operator_closed"  // operator_closed | no_operators
  }
}
```

### Сообщение оператора `POST /operator/send` (уже существует)

```json
{
  "session_id": "uuid-сессии-бота",
  "text": "Ответ оператора",
  "operator_name": "Алина",
  "operator_id": 1
}
```

---

## Изменения в проекте Bot (этот репозиторий)

| Файл | Что меняется |
|------|-------------|
| `app/config.py` | +5 настроек: `CRM_ENABLED`, `CRM_BASE_URL`, `CRM_API_KEY`, `CRM_WEBHOOK_SECRET`, `CRM_CONTEXT_MESSAGES` |
| `app/db/models.py` | +2 поля в ChatSession: `crm_chat_id`, `crm_queue_position` + alembic миграция |
| `app/services/crm_client.py` | **НОВЫЙ** — HTTP клиент CRM (httpx) |
| `app/api/fastapi_app.py` | +endpoint `POST /crm/webhook`, +optional `crm_chat_id` в `/operator/send` |
| `app/bot/handlers/commands.py` | `enable_human_mode` → CRM create_chat. `disable_human_mode` → CRM close |
| `app/services/chat_service.py` | human_mode: пересылка в CRM. Выключение: close_chat в CRM |
| `app/bot/i18n.py` | +5 строк (searching_operator, queue_position, operator_assigned, no_operators, crm_unavailable) |

## Обратная совместимость

- `CRM_ENABLED=false` (default) → работает как сейчас через OPERATOR_IDS в Telegram
- `CRM_ENABLED=true` → новый flow через CRM
- Fallback: если CRM недоступна → алерт операторам в Telegram (старое поведение)
- `/op`, `/operator/send`, SQLAdmin operator_reply — продолжают работать

---

## Edge cases

| Ситуация | Решение |
|----------|---------|
| User отменил пока в очереди | Bot → `close/` reason=`user_ended`, CRM удаляет из очереди, пересчёт позиций |
| Все операторы offline | CRM: status=queued, по таймауту fallback на Telegram |
| CRM недоступна | Fallback: алерт операторам в Telegram |
| Оператор не отвечает 10 мин | CRM переназначает или возвращает в очередь |
| Два worker'а → один чат | Redis lock + `select_for_update()` |
| Оператор вышел с активными чатами | `reassign_operator_chats` → в очередь или другому |
