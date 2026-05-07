"""OpenAI embeddings helpers for FAQ semantic search.

Two entry points:
 * ``embed_texts`` — async batch (used by /admin/seed and backfill)
 * ``embed_one_sync`` — synchronous single-text (used by SQLAlchemy event listeners)

Both swallow OpenAI errors and return ``None`` (or omit failed items): callers
are expected to fall back gracefully — never block FAQ writes on embedding
failure.
"""
from __future__ import annotations

import logging
import os
from typing import List, Optional

from app.config import get_settings

logger = logging.getLogger(__name__)


def _client_kwargs() -> dict | None:
    """Return AsyncOpenAI/OpenAI kwargs, or None when no API key is set.

    Callers should treat ``None`` as "feature unavailable" and skip the call.
    """
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        return None
    kwargs: dict = {
        "api_key": api_key,
        "timeout": float(os.getenv("OPENAI_REQUEST_TIMEOUT") or 15.0),
        "max_retries": int(os.getenv("OPENAI_MAX_RETRIES") or 1),
    }
    base_url = os.getenv("OPENAI_BASE_URL")
    if base_url:
        kwargs["base_url"] = base_url
    return kwargs


async def embed_texts(texts: List[str]) -> List[Optional[List[float]]]:
    """Batch-embed *texts*. Returns a list aligned with the input.

    On any OpenAI failure the entire batch is returned as ``[None, None, ...]``
    so the caller can decide how to proceed (skip, retry later, fallback).
    Callers should pass non-empty texts; empty strings receive ``None``.
    """
    settings = get_settings()
    if not settings.faq_embedding_enabled:
        return [None] * len(texts)
    if not texts:
        return []

    indexed = [(i, t) for i, t in enumerate(texts) if t and t.strip()]
    if not indexed:
        return [None] * len(texts)

    kwargs = _client_kwargs()
    if kwargs is None:
        return [None] * len(texts)

    try:
        from openai import AsyncOpenAI
    except ImportError:
        logger.error("openai package not installed — cannot compute embeddings")
        return [None] * len(texts)

    payload = [t for _, t in indexed]
    try:
        client = AsyncOpenAI(**kwargs)
    except Exception as exc:
        logger.warning("embed_texts client init failed: %s", exc)
        return [None] * len(texts)

    try:
        response = await client.embeddings.create(
            model=settings.faq_embedding_model,
            input=payload,
        )
    except Exception as exc:
        logger.warning("embed_texts failed (n=%d): %s", len(payload), exc)
        return [None] * len(texts)
    finally:
        try:
            await client.close()
        except Exception:
            pass

    out: list[Optional[list[float]]] = [None] * len(texts)
    for (orig_idx, _), record in zip(indexed, response.data):
        out[orig_idx] = list(record.embedding)
    return out


def embed_one_sync(text: str) -> Optional[List[float]]:
    """Synchronously embed a single text. Returns ``None`` on any failure.

    Used inside SQLAlchemy ``before_insert``/``before_update`` event listeners
    where async is awkward (we are inside a synchronous flush). The sync
    OpenAI client is created per-call — overhead is negligible compared to the
    network roundtrip itself, and this avoids holding a client across event
    loops.
    """
    settings = get_settings()
    if not settings.faq_embedding_enabled:
        return None
    if not text or not text.strip():
        return None

    kwargs = _client_kwargs()
    if kwargs is None:
        return None

    try:
        from openai import OpenAI
    except ImportError:
        logger.error("openai package not installed — cannot compute embedding")
        return None

    try:
        client = OpenAI(**kwargs)
    except Exception as exc:
        logger.warning("embed_one_sync client init failed: %s", exc)
        return None

    try:
        response = client.embeddings.create(
            model=settings.faq_embedding_model,
            input=[text],
        )
        return list(response.data[0].embedding)
    except Exception as exc:
        logger.warning("embed_one_sync failed: %s", exc)
        return None
    finally:
        try:
            client.close()
        except Exception:
            pass
