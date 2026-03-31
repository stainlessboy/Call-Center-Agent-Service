"""Dashboard analytics queries."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any

from sqlalchemy import Float, Integer, and_, case, cast, distinct, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ChatSession, Lead, Message, SessionStatus, User


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _days_ago(n: int) -> datetime:
    return _utc_now() - timedelta(days=n)


async def get_overview_cards(session: AsyncSession) -> dict[str, Any]:
    """Top-level KPI cards: sessions, users, messages, leads, LLM cost."""
    today_start = _utc_now().replace(hour=0, minute=0, second=0, microsecond=0)
    week_start = today_start - timedelta(days=today_start.weekday())
    month_start = today_start.replace(day=1)

    # Sessions
    sessions_today = (await session.execute(
        select(func.count()).where(ChatSession.started_at >= today_start)
    )).scalar() or 0
    sessions_week = (await session.execute(
        select(func.count()).where(ChatSession.started_at >= week_start)
    )).scalar() or 0
    sessions_month = (await session.execute(
        select(func.count()).where(ChatSession.started_at >= month_start)
    )).scalar() or 0
    active_sessions = (await session.execute(
        select(func.count()).where(ChatSession.status == SessionStatus.ACTIVE)
    )).scalar() or 0

    # Unique users today
    users_today = (await session.execute(
        select(func.count(distinct(ChatSession.user_id))).where(
            ChatSession.started_at >= today_start
        )
    )).scalar() or 0

    # Total users
    total_users = (await session.execute(select(func.count()).select_from(User))).scalar() or 0

    # Messages
    messages_today = (await session.execute(
        select(func.count()).where(Message.created_at >= today_start)
    )).scalar() or 0

    # Leads
    leads_today = (await session.execute(
        select(func.count()).where(Lead.created_at >= today_start)
    )).scalar() or 0
    leads_month = (await session.execute(
        select(func.count()).where(Lead.created_at >= month_start)
    )).scalar() or 0

    # LLM cost
    cost_today = (await session.execute(
        select(func.coalesce(func.sum(Message.llm_cost), 0.0)).where(
            Message.created_at >= today_start, Message.llm_cost.isnot(None)
        )
    )).scalar() or 0.0
    cost_week = (await session.execute(
        select(func.coalesce(func.sum(Message.llm_cost), 0.0)).where(
            Message.created_at >= week_start, Message.llm_cost.isnot(None)
        )
    )).scalar() or 0.0
    cost_month = (await session.execute(
        select(func.coalesce(func.sum(Message.llm_cost), 0.0)).where(
            Message.created_at >= month_start, Message.llm_cost.isnot(None)
        )
    )).scalar() or 0.0

    return {
        "sessions_today": sessions_today,
        "sessions_week": sessions_week,
        "sessions_month": sessions_month,
        "active_sessions": active_sessions,
        "users_today": users_today,
        "total_users": total_users,
        "messages_today": messages_today,
        "leads_today": leads_today,
        "leads_month": leads_month,
        "cost_today": round(cost_today, 4),
        "cost_week": round(cost_week, 4),
        "cost_month": round(cost_month, 4),
    }


async def get_llm_daily_stats(session: AsyncSession, days: int = 30) -> list[dict]:
    """Token usage and cost per day."""
    since = _days_ago(days)
    stmt = (
        select(
            func.date(Message.created_at).label("day"),
            func.coalesce(func.sum(Message.prompt_tokens), 0).label("prompt_tokens"),
            func.coalesce(func.sum(Message.completion_tokens), 0).label("completion_tokens"),
            func.coalesce(func.sum(Message.total_tokens), 0).label("total_tokens"),
            func.coalesce(func.sum(Message.llm_cost), 0.0).label("cost"),
            func.count().filter(Message.llm_cost.isnot(None)).label("llm_calls"),
        )
        .where(Message.created_at >= since, Message.role == "agent")
        .group_by(func.date(Message.created_at))
        .order_by(func.date(Message.created_at))
    )
    rows = (await session.execute(stmt)).all()
    return [
        {
            "day": str(r.day),
            "prompt_tokens": r.prompt_tokens,
            "completion_tokens": r.completion_tokens,
            "total_tokens": r.total_tokens,
            "cost": round(float(r.cost), 6),
            "llm_calls": r.llm_calls,
        }
        for r in rows
    ]


async def get_latency_daily_stats(session: AsyncSession, days: int = 30) -> list[dict]:
    """Average latency per day."""
    since = _days_ago(days)
    stmt = (
        select(
            func.date(Message.created_at).label("day"),
            func.round(cast(func.avg(Message.latency_ms), Float), 0).label("avg_latency"),
            func.max(Message.latency_ms).label("max_latency"),
            func.min(Message.latency_ms).label("min_latency"),
        )
        .where(
            Message.created_at >= since,
            Message.role == "agent",
            Message.latency_ms.isnot(None),
        )
        .group_by(func.date(Message.created_at))
        .order_by(func.date(Message.created_at))
    )
    rows = (await session.execute(stmt)).all()
    return [
        {
            "day": str(r.day),
            "avg_latency": int(r.avg_latency or 0),
            "max_latency": r.max_latency or 0,
            "min_latency": r.min_latency or 0,
        }
        for r in rows
    ]


async def get_users_daily_stats(session: AsyncSession, days: int = 30) -> list[dict]:
    """New users and active users per day."""
    since = _days_ago(days)
    # New users per day
    new_users_stmt = (
        select(
            func.date(User.created_at).label("day"),
            func.count().label("new_users"),
        )
        .where(User.created_at >= since)
        .group_by(func.date(User.created_at))
        .order_by(func.date(User.created_at))
    )
    new_rows = (await session.execute(new_users_stmt)).all()

    # Active users per day (had a session)
    active_stmt = (
        select(
            func.date(ChatSession.last_activity_at).label("day"),
            func.count(distinct(ChatSession.user_id)).label("active_users"),
        )
        .where(ChatSession.last_activity_at >= since)
        .group_by(func.date(ChatSession.last_activity_at))
        .order_by(func.date(ChatSession.last_activity_at))
    )
    active_rows = (await session.execute(active_stmt)).all()

    # Merge
    data: dict[str, dict] = {}
    for r in new_rows:
        data.setdefault(str(r.day), {"day": str(r.day), "new_users": 0, "active_users": 0})
        data[str(r.day)]["new_users"] = r.new_users
    for r in active_rows:
        data.setdefault(str(r.day), {"day": str(r.day), "new_users": 0, "active_users": 0})
        data[str(r.day)]["active_users"] = r.active_users

    return sorted(data.values(), key=lambda x: x["day"])


async def get_language_distribution(session: AsyncSession) -> list[dict]:
    """User language distribution."""
    stmt = (
        select(
            func.coalesce(User.language, "unknown").label("language"),
            func.count().label("count"),
        )
        .group_by(func.coalesce(User.language, "unknown"))
        .order_by(func.count().desc())
    )
    rows = (await session.execute(stmt)).all()
    return [{"language": r.language, "count": r.count} for r in rows]


async def get_session_stats(session: AsyncSession) -> dict:
    """Average messages per session, average session duration."""
    # Avg messages per session (last 30 days)
    since = _days_ago(30)
    msg_count_stmt = (
        select(
            func.avg(func.count()).over().label("avg_messages"),
        )
        .where(Message.created_at >= since)
        .group_by(Message.session_id)
    )
    # Simpler approach
    total_msgs = (await session.execute(
        select(func.count()).where(Message.created_at >= since, Message.role.in_(("user", "agent")))
    )).scalar() or 0
    total_sessions = (await session.execute(
        select(func.count(distinct(Message.session_id))).where(Message.created_at >= since)
    )).scalar() or 1

    # Average session duration (for ended sessions)
    duration_stmt = (
        select(
            func.avg(
                func.extract("epoch", ChatSession.ended_at) - func.extract("epoch", ChatSession.started_at)
            ).label("avg_duration_seconds"),
        )
        .where(
            ChatSession.ended_at.isnot(None),
            ChatSession.started_at >= since,
        )
    )
    avg_duration = (await session.execute(duration_stmt)).scalar() or 0

    return {
        "avg_messages_per_session": round(total_msgs / max(total_sessions, 1), 1),
        "avg_session_duration_minutes": round(float(avg_duration) / 60, 1) if avg_duration else 0,
        "total_sessions_30d": total_sessions,
    }


async def get_leads_by_category(session: AsyncSession) -> list[dict]:
    """Leads grouped by product category."""
    stmt = (
        select(
            func.coalesce(Lead.product_category, "unknown").label("category"),
            func.count().label("count"),
        )
        .group_by(func.coalesce(Lead.product_category, "unknown"))
        .order_by(func.count().desc())
    )
    rows = (await session.execute(stmt)).all()
    return [{"category": r.category, "count": r.count} for r in rows]


async def get_leads_by_status(session: AsyncSession) -> list[dict]:
    """Leads grouped by status."""
    stmt = (
        select(Lead.status, func.count().label("count"))
        .group_by(Lead.status)
        .order_by(func.count().desc())
    )
    rows = (await session.execute(stmt)).all()
    return [{"status": r.status, "count": r.count} for r in rows]


async def get_leads_daily(session: AsyncSession, days: int = 30) -> list[dict]:
    """Leads per day."""
    since = _days_ago(days)
    stmt = (
        select(
            func.date(Lead.created_at).label("day"),
            func.count().label("count"),
        )
        .where(Lead.created_at >= since)
        .group_by(func.date(Lead.created_at))
        .order_by(func.date(Lead.created_at))
    )
    rows = (await session.execute(stmt)).all()
    return [{"day": str(r.day), "count": r.count} for r in rows]


async def get_operator_stats(session: AsyncSession, days: int = 30) -> dict:
    """Operator handoff statistics."""
    since = _days_ago(days)

    # Human mode sessions per day
    human_daily_stmt = (
        select(
            func.date(ChatSession.human_mode_since).label("day"),
            func.count().label("count"),
        )
        .where(
            ChatSession.human_mode_since.isnot(None),
            ChatSession.human_mode_since >= since,
        )
        .group_by(func.date(ChatSession.human_mode_since))
        .order_by(func.date(ChatSession.human_mode_since))
    )
    daily_rows = (await session.execute(human_daily_stmt)).all()

    # Closed reasons
    closed_stmt = (
        select(
            func.coalesce(ChatSession.closed_reason, "active").label("reason"),
            func.count().label("count"),
        )
        .where(ChatSession.started_at >= since)
        .group_by(func.coalesce(ChatSession.closed_reason, "active"))
        .order_by(func.count().desc())
    )
    closed_rows = (await session.execute(closed_stmt)).all()

    # Total human mode sessions
    total_human = (await session.execute(
        select(func.count()).where(
            ChatSession.human_mode_since.isnot(None),
            ChatSession.started_at >= since,
        )
    )).scalar() or 0

    total_sessions = (await session.execute(
        select(func.count()).where(ChatSession.started_at >= since)
    )).scalar() or 1

    return {
        "human_daily": [{"day": str(r.day), "count": r.count} for r in daily_rows],
        "closed_reasons": [{"reason": r.reason, "count": r.count} for r in closed_rows],
        "total_human_sessions": total_human,
        "human_rate_pct": round(total_human / max(total_sessions, 1) * 100, 1),
    }


async def get_quality_stats(session: AsyncSession) -> dict:
    """Bot quality metrics: feedback, fallback rate, error rate."""
    since = _days_ago(30)

    # Feedback distribution
    feedback_stmt = (
        select(
            ChatSession.feedback_rating,
            func.count().label("count"),
        )
        .where(
            ChatSession.feedback_rating.isnot(None),
            ChatSession.started_at >= since,
        )
        .group_by(ChatSession.feedback_rating)
        .order_by(ChatSession.feedback_rating)
    )
    feedback_rows = (await session.execute(feedback_stmt)).all()

    # Average rating
    avg_rating = (await session.execute(
        select(func.avg(ChatSession.feedback_rating)).where(
            ChatSession.feedback_rating.isnot(None),
            ChatSession.started_at >= since,
        )
    )).scalar()

    # Error messages count
    total_agent_msgs = (await session.execute(
        select(func.count()).where(Message.role == "agent", Message.created_at >= since)
    )).scalar() or 1
    error_msgs = (await session.execute(
        select(func.count()).where(
            Message.error_code.isnot(None),
            Message.created_at >= since,
        )
    )).scalar() or 0

    # Human mode rate (proxy for bot inability)
    total_sessions = (await session.execute(
        select(func.count()).where(ChatSession.started_at >= since)
    )).scalar() or 1
    human_sessions = (await session.execute(
        select(func.count()).where(
            ChatSession.human_mode_since.isnot(None),
            ChatSession.started_at >= since,
        )
    )).scalar() or 0

    # Sessions with feedback
    sessions_with_feedback = (await session.execute(
        select(func.count()).where(
            ChatSession.feedback_rating.isnot(None),
            ChatSession.started_at >= since,
        )
    )).scalar() or 0

    return {
        "feedback_distribution": [
            {"rating": r.feedback_rating, "count": r.count} for r in feedback_rows
        ],
        "avg_rating": round(float(avg_rating), 2) if avg_rating else None,
        "sessions_with_feedback": sessions_with_feedback,
        "error_rate_pct": round(error_msgs / max(total_agent_msgs, 1) * 100, 1),
        "human_mode_rate_pct": round(human_sessions / max(total_sessions, 1) * 100, 1),
    }


async def get_conversion_stats(session: AsyncSession) -> dict:
    """Session → lead conversion."""
    since = _days_ago(30)
    total_sessions = (await session.execute(
        select(func.count()).where(ChatSession.started_at >= since)
    )).scalar() or 1
    total_leads = (await session.execute(
        select(func.count()).where(Lead.created_at >= since)
    )).scalar() or 0

    return {
        "total_sessions": total_sessions,
        "total_leads": total_leads,
        "conversion_pct": round(total_leads / max(total_sessions, 1) * 100, 2),
    }


async def get_full_dashboard(session: AsyncSession) -> dict[str, Any]:
    """Aggregate all dashboard data in one call."""
    overview = await get_overview_cards(session)
    llm_daily = await get_llm_daily_stats(session)
    latency_daily = await get_latency_daily_stats(session)
    users_daily = await get_users_daily_stats(session)
    lang_dist = await get_language_distribution(session)
    session_stats = await get_session_stats(session)
    leads_category = await get_leads_by_category(session)
    leads_status = await get_leads_by_status(session)
    leads_daily = await get_leads_daily(session)
    operator = await get_operator_stats(session)
    quality = await get_quality_stats(session)
    conversion = await get_conversion_stats(session)

    return {
        "overview": overview,
        "llm_daily": llm_daily,
        "latency_daily": latency_daily,
        "users_daily": users_daily,
        "language_distribution": lang_dist,
        "session_stats": session_stats,
        "leads_by_category": leads_category,
        "leads_by_status": leads_status,
        "leads_daily": leads_daily,
        "operator": operator,
        "quality": quality,
        "conversion": conversion,
    }
