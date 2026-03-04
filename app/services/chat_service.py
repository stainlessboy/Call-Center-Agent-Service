from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.bot.i18n import normalize_lang, t
from app.db.models import Branch, ChatSession, Message, SessionStatus, User
from app.services.agent_client import AgentClient
from app.services.agent import AgentTurnResult as _AgentTurnResult  # noqa: F401

logger = logging.getLogger(__name__)
PDF_MARKER_RE = re.compile(r"\[\[PDF:(.+?)\]\]")


@dataclass
class AgentReply:
    text: str
    pdf_path: Optional[str] = None
    session_id: Optional[str] = None
    human_mode: bool = False
    keyboard_options: Optional[list] = None
    show_operator_button: bool = False


class ChatService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        agent_client: AgentClient,
        operator_ids: Optional[list[int]] = None,
    ):
        self.session_factory = session_factory
        self.agent_client = agent_client
        self.operator_ids = operator_ids or []

    async def get_or_create_user(
        self,
        telegram_user_id: int,
        username: Optional[str],
        first_name: Optional[str],
        last_name: Optional[str],
        phone: Optional[str] = None,
        language: Optional[str] = None,
    ) -> User:
        async with self.session_factory() as session:
            async with session.begin():
                result = await session.execute(
                    select(User).where(User.telegram_user_id == telegram_user_id)
                )
                user = result.scalar_one_or_none()
                if user is None:
                    user = User(
                        telegram_user_id=telegram_user_id,
                        username=username,
                        first_name=first_name,
                        last_name=last_name,
                        phone=phone,
                        language=language,
                    )
                    session.add(user)
                else:
                    await session.execute(
                        update(User)
                        .where(User.id == user.id)
                        .values(
                            username=username or user.username,
                            first_name=first_name or user.first_name,
                            last_name=last_name or user.last_name,
                            phone=phone or user.phone,
                            language=language or user.language,
                        )
                    )
            await session.commit()
            await session.refresh(user)
            return user

    async def _get_active_session(self, session: AsyncSession, user_id: int) -> Optional[ChatSession]:
        result = await session.execute(
            select(ChatSession)
            .where(ChatSession.user_id == user_id, ChatSession.status == SessionStatus.ACTIVE)
            .order_by(ChatSession.last_activity_at.desc(), ChatSession.started_at.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def ensure_active_session(self, user_id: int) -> ChatSession:
        async with self.session_factory() as session:
            async with session.begin():
                chat_session = await self._get_active_session(session, user_id)
                if chat_session is None:
                    chat_session = ChatSession(
                        user_id=user_id,
                        status=SessionStatus.ACTIVE,
                        last_activity_at=datetime.now(timezone.utc),
                    )
                    session.add(chat_session)
                else:
                    chat_session.last_activity_at = datetime.now(timezone.utc)
            await session.commit()
            await session.refresh(chat_session)
            return chat_session

    async def start_new_session(self, user_id: int) -> ChatSession:
        async with self.session_factory() as session:
            async with session.begin():
                chat_session = ChatSession(
                    user_id=user_id,
                    status=SessionStatus.ACTIVE,
                    last_activity_at=datetime.now(timezone.utc),
                    human_mode=False,
                    human_mode_since=None,
                    assigned_operator_id=None,
                )
                session.add(chat_session)
            await session.commit()
            await session.refresh(chat_session)
            return chat_session

    async def list_active_sessions(self, user_id: int, limit: int = 10) -> list[ChatSession]:
        async with self.session_factory() as session:
            result = await session.execute(
                select(ChatSession)
                .where(ChatSession.user_id == user_id, ChatSession.status == SessionStatus.ACTIVE)
                .order_by(ChatSession.last_activity_at.desc(), ChatSession.started_at.desc())
                .limit(limit)
            )
            return list(result.scalars().all())

    async def switch_active_session(self, user_id: int, session_id: str) -> Optional[ChatSession]:
        session_id = (session_id or "").strip()
        if not session_id:
            return None
        async with self.session_factory() as session:
            async with session.begin():
                result = await session.execute(
                    select(ChatSession)
                    .where(
                        ChatSession.id == session_id,
                        ChatSession.user_id == user_id,
                        ChatSession.status == SessionStatus.ACTIVE,
                    )
                    .limit(1)
                )
                chat_session = result.scalar_one_or_none()
                if chat_session is None:
                    return None
                chat_session.last_activity_at = datetime.now(timezone.utc)
            await session.commit()
            await session.refresh(chat_session)
            return chat_session

    async def end_active_session(self, user_id: int) -> Optional[str]:
        async with self.session_factory() as session:
            async with session.begin():
                chat_session = await self._get_active_session(session, user_id)
                if chat_session is None:
                    return None
                chat_session.status = SessionStatus.ENDED
                chat_session.ended_at = datetime.now(timezone.utc)
                chat_session.closed_reason = "manual_end"
            await session.commit()
            return chat_session.id

    async def _save_message(
        self,
        session_id: str,
        role: str,
        text: str,
        telegram_message_id: Optional[int] = None,
        latency_ms: Optional[int] = None,
        agent_model: Optional[str] = None,
        error_code: Optional[str] = None,
    ) -> None:
        async with self.session_factory() as session:
            async with session.begin():
                message = Message(
                    session_id=session_id,
                    role=role,
                    text=text,
                    telegram_message_id=str(telegram_message_id) if telegram_message_id else None,
                    latency_ms=latency_ms,
                    agent_model=agent_model,
                    error_code=error_code,
                )
                session.add(message)
            await session.commit()

    async def handle_user_message(
        self,
        user: User,
        text: str,
        telegram_message_id: Optional[int] = None,
    ) -> AgentReply:
        chat_session = await self.ensure_active_session(user.id)
        # Set session title from the first user message if it's empty.
        await self.set_session_title_if_empty(chat_session.id, text)
        await self.touch_session(chat_session.id)
        await self._save_message(
            session_id=chat_session.id,
            role="user",
            text=text,
            telegram_message_id=telegram_message_id,
        )

        if chat_session.human_mode:
            # Route through LangGraph interrupt so operator reply can resume the graph
            try:
                await self.agent_client.send_message(
                    session_id=chat_session.id,
                    user_id=user.telegram_user_id,
                    text=text,
                    language=normalize_lang(user.language),
                    human_mode=True,
                )
            except Exception:
                pass
            return AgentReply(
                text=t("sent_to_operator", user.language),
                pdf_path=None,
                session_id=chat_session.id,
                human_mode=True,
            )

        _unavailable_text = t("agent_unavailable", user.language)
        agent_text = _unavailable_text
        agent_keyboard: Optional[list] = None
        latency_ms: Optional[int] = None
        try:
            started = time.perf_counter()
            turn_result = await asyncio.wait_for(
                self.agent_client.send_message(
                    session_id=chat_session.id,
                    user_id=user.telegram_user_id,
                    text=text,
                    language=normalize_lang(user.language),
                ),
                timeout=25.0,
            )
            agent_text = turn_result.text
            agent_keyboard = turn_result.keyboard_options
            latency_ms = int((time.perf_counter() - started) * 1000)
        except asyncio.TimeoutError:
            logger.warning("Agent timed out for session %s", chat_session.id)
            await self._save_message(
                session_id=chat_session.id,
                role="system",
                text="Таймаут агента",
                error_code="agent_timeout",
            )
            return AgentReply(text=t("agent_unavailable", user.language))
        except Exception as exc:  # pragma: no cover - network failure path
            logger.exception("Agent request failed: %s", exc)
            await self._save_message(
                session_id=chat_session.id,
                role="system",
                text="Агент временно недоступен",
                error_code="agent_unavailable",
            )
            return AgentReply(text=agent_text)

        pdf_path: Optional[str] = None
        match = PDF_MARKER_RE.search(agent_text)
        if match:
            pdf_path = match.group(1).strip()
            agent_text = PDF_MARKER_RE.sub("", agent_text).strip()

        if agent_text:
            try:
                agent_text = await self.agent_client.ensure_language(
                    text=agent_text,
                    language=normalize_lang(user.language),
                )
            except Exception:  # pragma: no cover - translation fallback
                logger.exception("Agent reply language normalization failed")

        await self._save_message(
            session_id=chat_session.id,
            role="agent",
            text=agent_text,
            latency_ms=latency_ms,
        )
        return AgentReply(
            text=agent_text,
            pdf_path=pdf_path,
            session_id=chat_session.id,
            human_mode=chat_session.human_mode,
            keyboard_options=agent_keyboard,
            show_operator_button=getattr(turn_result, "show_operator_button", False),
        )

    async def close_inactive_sessions(self, timeout_minutes: int) -> list[tuple[User, ChatSession]]:
        if timeout_minutes <= 0:
            return []
        threshold = datetime.now(timezone.utc) - timedelta(minutes=timeout_minutes)
        closed: list[tuple[User, ChatSession]] = []
        async with self.session_factory() as session:
            async with session.begin():
                result = await session.execute(
                    select(ChatSession, User)
                    .join(User, ChatSession.user_id == User.id)
                    .where(
                        ChatSession.status == SessionStatus.ACTIVE,
                        ChatSession.last_activity_at <= threshold,
                    )
                )
                rows = result.all()
                for chat_session, user in rows:
                    chat_session.status = SessionStatus.ENDED
                    chat_session.ended_at = datetime.now(timezone.utc)
                    chat_session.closed_reason = "timeout"
                    closed.append((user, chat_session))
        return closed

    async def return_stale_human_sessions_to_bot(self, timeout_minutes: int) -> list[tuple[User, ChatSession]]:
        if timeout_minutes <= 0:
            return []
        threshold = datetime.now(timezone.utc) - timedelta(minutes=timeout_minutes)
        switched: list[tuple[User, ChatSession]] = []
        sync_targets: list[tuple[str, datetime]] = []
        async with self.session_factory() as session:
            async with session.begin():
                result = await session.execute(
                    select(ChatSession, User)
                    .join(User, ChatSession.user_id == User.id)
                    .where(
                        ChatSession.status == SessionStatus.ACTIVE,
                        ChatSession.human_mode.is_(True),
                        ChatSession.human_mode_since.is_not(None),
                        ChatSession.human_mode_since <= threshold,
                    )
                )
                rows = result.all()
                for chat_session, user in rows:
                    since = chat_session.human_mode_since
                    if since is None:
                        continue
                    operator_msg_exists = await session.execute(
                        select(Message.id)
                        .where(
                            Message.session_id == chat_session.id,
                            Message.role == "operator",
                            Message.created_at >= since,
                        )
                        .limit(1)
                    )
                    if operator_msg_exists.scalar_one_or_none() is not None:
                        continue
                    chat_session.human_mode = False
                    chat_session.human_mode_since = None
                    chat_session.assigned_operator_id = None
                    chat_session.last_activity_at = datetime.now(timezone.utc)
                    switched.append((user, chat_session))
                    sync_targets.append((chat_session.id, since))
                    session.add(
                        Message(
                            session_id=chat_session.id,
                            role="system",
                            text="Возврат в режим бота: оператор не ответил в течение заданного времени.",
                            error_code="human_mode_timeout",
                        )
                    )
        for sync_session_id, since in sync_targets:
            await self.sync_human_mode_history_to_agent(session_id=sync_session_id, since=since)
        return switched

    async def record_feedback(self, session_id: str, rating: int, comment: Optional[str] = None) -> bool:
        async with self.session_factory() as session:
            async with session.begin():
                result = await session.execute(select(ChatSession).where(ChatSession.id == session_id))
                chat_session = result.scalar_one_or_none()
                if chat_session is None:
                    return False
                chat_session.feedback_rating = rating
                chat_session.feedback_comment = comment
        return True

    async def touch_session(self, session_id: str) -> None:
        async with self.session_factory() as session:
            async with session.begin():
                await session.execute(
                    update(ChatSession)
                    .where(ChatSession.id == session_id)
                    .values(last_activity_at=datetime.now(timezone.utc))
                )

    async def list_recent_sessions(self, user_id: int, limit: int = 5) -> list[ChatSession]:
        async with self.session_factory() as session:
            result = await session.execute(
                select(ChatSession)
                .where(ChatSession.user_id == user_id)
                .order_by(ChatSession.started_at.desc())
                .limit(limit)
            )
            return list(result.scalars().all())

    async def set_session_title_if_empty(self, session_id: str, text: str) -> None:
        title = (text or "").strip()
        if not title:
            return
        title = title[:70]
        async with self.session_factory() as session:
            async with session.begin():
                await session.execute(
                    update(ChatSession)
                    .where(ChatSession.id == session_id, ChatSession.title.is_(None))
                    .values(title=title)
                )

    async def set_human_mode(
        self,
        session_id: str,
        enabled: bool,
        assigned_operator_id: Optional[int] = None,
    ) -> Optional[ChatSession]:
        sync_since: Optional[datetime] = None
        async with self.session_factory() as session:
            async with session.begin():
                result = await session.execute(
                    select(ChatSession).where(ChatSession.id == session_id)
                )
                chat_session = result.scalar_one_or_none()
                if chat_session is None:
                    return None
                if not enabled and chat_session.human_mode and chat_session.human_mode_since is not None:
                    sync_since = chat_session.human_mode_since
                chat_session.human_mode = enabled
                chat_session.human_mode_since = datetime.now(timezone.utc) if enabled else None
                if assigned_operator_id is not None:
                    chat_session.assigned_operator_id = assigned_operator_id
                elif not enabled:
                    chat_session.assigned_operator_id = None
                chat_session.last_activity_at = datetime.now(timezone.utc)
        if not enabled and sync_since is not None:
            await self.sync_human_mode_history_to_agent(session_id=session_id, since=sync_since)
        return chat_session

    async def sync_human_mode_history_to_agent(self, session_id: str, since: Optional[datetime]) -> int:
        if since is None:
            return 0
        async with self.session_factory() as session:
            result = await session.execute(
                select(Message.role, Message.text)
                .where(
                    Message.session_id == session_id,
                    Message.created_at >= since,
                    Message.role.in_(("user", "operator")),
                )
                .order_by(Message.created_at.asc(), Message.id.asc())
            )
            rows = result.all()

        events: list[dict[str, str]] = []
        for role, text in rows:
            payload = (text or "").strip()
            if not payload:
                continue
            if role == "user":
                events.append({"role": "user", "text": payload})
            elif role == "operator":
                # Operator responses are injected as assistant replies,
                # so the bot can continue from the same conversation context.
                events.append({"role": "assistant", "text": payload})

        if not events:
            return 0

        try:
            await self.agent_client.sync_history(session_id=session_id, events=events)
        except Exception as exc:
            logger.exception("Failed to sync human-mode history to agent for session=%s: %s", session_id, exc)
            return 0
        return len(events)

    async def get_recent_messages(
        self, session_id: str, limit: int = 10
    ) -> list[Message]:
        """Return the last `limit` user/agent messages from a session, oldest first."""
        async with self.session_factory() as session:
            result = await session.execute(
                select(Message)
                .where(
                    Message.session_id == session_id,
                    Message.role.in_(("user", "agent")),
                )
                .order_by(Message.created_at.desc(), Message.id.desc())
                .limit(limit)
            )
            rows = result.scalars().all()
        return list(reversed(rows))

    async def get_session_with_user(self, session_id: str) -> Optional[tuple[ChatSession, User]]:
        async with self.session_factory() as session:
            result = await session.execute(
                select(ChatSession, User)
                .join(User, ChatSession.user_id == User.id)
                .where(ChatSession.id == session_id)
            )
            row = result.one_or_none()
            return (row[0], row[1]) if row else None

    async def list_human_sessions(self, limit: int = 10) -> list[tuple[ChatSession, User]]:
        async with self.session_factory() as session:
            result = await session.execute(
                select(ChatSession, User)
                .join(User, ChatSession.user_id == User.id)
                .where(
                    ChatSession.status == SessionStatus.ACTIVE,
                    ChatSession.human_mode.is_(True),
                )
                .order_by(ChatSession.human_mode_since.desc().nullslast())
                .limit(limit)
            )
            return [(row[0], row[1]) for row in result.all()]

    async def send_operator_message(
        self,
        session_id: str,
        operator_telegram_id: int,
        text: str,
    ) -> Optional[int]:
        """
        Saves operator message into DB and returns target user chat id.
        """
        async with self.session_factory() as session:
            async with session.begin():
                result = await session.execute(
                    select(ChatSession, User)
                    .join(User, ChatSession.user_id == User.id)
                    .where(ChatSession.id == session_id)
                )
                row = result.one_or_none()
                if row is None:
                    return None
                chat_session, user = row
                if chat_session.status != SessionStatus.ACTIVE:
                    return None
                chat_session.human_mode = True
                chat_session.human_mode_since = chat_session.human_mode_since or datetime.now(timezone.utc)
                if chat_session.assigned_operator_id is None:
                    chat_session.assigned_operator_id = operator_telegram_id
                chat_session.last_activity_at = datetime.now(timezone.utc)
                message = Message(
                    session_id=chat_session.id,
                    role="operator",
                    text=text,
                    latency_ms=None,
                    agent_model=None,
                    error_code=None,
                )
                session.add(message)
            await session.commit()
            # After saving operator message, try to resume the LangGraph session
            try:
                await self.agent_client.resume_human_mode(str(chat_session.id), text)
            except Exception:
                pass
            return user.telegram_user_id

    async def list_regions(self) -> list[str]:
        async with self.session_factory() as session:
            result = await session.execute(select(Branch.region).distinct())
            regions = [r[0] for r in result.all() if r[0]]
            return sorted(regions)

    async def list_districts(self, region: str | None = None) -> list[str]:
        async with self.session_factory() as session:
            stmt = select(Branch.district).distinct()
            if region:
                stmt = stmt.where(Branch.region == region)
            result = await session.execute(stmt)
            districts = [r[0] for r in result.all() if r[0]]
            return sorted(districts)

    async def list_branches(self, region: str | None = None, district: str | None = None) -> list[Branch]:
        async with self.session_factory() as session:
            stmt = select(Branch)
            if region:
                stmt = stmt.where(Branch.region == region)
            if district:
                stmt = stmt.where(Branch.district == district)
            stmt = stmt.order_by(Branch.name)
            result = await session.execute(stmt)
            return list(result.scalars().all())
