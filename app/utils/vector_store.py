"""Weaviate-backed FAQ index — the only place that talks to Weaviate.

Replaces the pgvector embedding columns on ``faq``. Postgres stays the source
of truth for the FAQ *text* (admin CRUD, xlsx import/export); this module keeps
a searchable mirror that can be rebuilt from Postgres at any time via
:func:`reindex`.

Two things moved out of the application and into Weaviate:

* **Vectorization** — the collection declares ``text2vec-openai``, so Weaviate
  embeds ``question`` itself on write. Nothing here computes embeddings.
* **Lexical scoring** — BM25 is built in, so :func:`search` returns one hybrid
  ranking instead of the two hand-rolled legs that used to be fused in
  ``faq_tools``.

Reranking deliberately stayed in the application (``faq_tools``): Weaviate's
local ``reranker-transformers`` module only ships English ``ms-marco``
cross-encoders, which collapse on Cyrillic (measured 3/18 on a labelled
Russian set, against 18/18 for a ``gpt-4o-mini`` rerank).

Every public function swallows Weaviate failures and degrades to "no result"
(``[]`` / ``False``) rather than raising: a FAQ search that cannot reach the
index must never break the customer's turn, and a FAQ row must still be
writable in the admin when the index is down. Use :func:`reindex` to repair
the mirror afterwards.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Iterable, Optional, Sequence

from app.config import get_settings

logger = logging.getLogger(__name__)

LANGS = ("ru", "en", "uz")


@dataclass(frozen=True)
class FaqHit:
    """One candidate returned by the hybrid search."""

    faq_id: int
    lang: str
    question: str
    answer: str
    score: float


# ---------------------------------------------------------------------------
# Client lifecycle
# ---------------------------------------------------------------------------
# One async client per process, opened in the FastAPI lifespan. The gRPC
# channel is expensive to build, so it is created once and reused; the lock
# keeps concurrent turns from racing to open a second one.

_client: Any = None
_client_lock = asyncio.Lock()
_schema_ready = False


def _auth():
    settings = get_settings()
    if not settings.weaviate_api_key:
        return None
    from weaviate.classes.init import Auth

    return Auth.api_key(settings.weaviate_api_key)


def _additional_config():
    from weaviate.classes.init import AdditionalConfig, Timeout

    t = get_settings().weaviate_timeout
    return AdditionalConfig(timeout=Timeout(init=t, query=t, insert=t * 3))


async def get_client() -> Any:
    """Return the shared async client, connecting on first use.

    Returns ``None`` when Weaviate is disabled or unreachable — callers treat
    that as "index unavailable" and fall back.
    """
    global _client
    settings = get_settings()
    if not settings.weaviate_enabled:
        return None
    if _client is not None:
        return _client

    async with _client_lock:
        if _client is not None:
            return _client
        try:
            import weaviate

            client = weaviate.use_async_with_custom(
                http_host=settings.weaviate_http_host,
                http_port=settings.weaviate_http_port,
                http_secure=settings.weaviate_secure,
                grpc_host=settings.weaviate_grpc_host,
                grpc_port=settings.weaviate_grpc_port,
                grpc_secure=settings.weaviate_secure,
                auth_credentials=_auth(),
                additional_config=_additional_config(),
                # Startup must not hard-fail on a slow/absent Weaviate — the
                # app is expected to run (lexically degraded) without it.
                skip_init_checks=True,
            )
            await client.connect()
        except Exception:
            logger.warning("weaviate connect failed — FAQ semantic search disabled", exc_info=True)
            return None
        _client = client
        return _client


async def close() -> None:
    """Close the shared client. Called from the FastAPI lifespan shutdown."""
    global _client, _schema_ready
    client, _client, _schema_ready = _client, None, False
    if client is None:
        return
    try:
        await client.close()
    except Exception:
        logger.debug("weaviate close failed", exc_info=True)


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

async def ensure_schema() -> bool:
    """Create the collection if missing. Idempotent; safe to call on boot.

    One object per (faq_id, lang) rather than one per row with three named
    vectors: the language fallback ("search the user's language *and* ru") is
    then a single ``lang`` filter on one query instead of two queries merged
    by hand.
    """
    global _schema_ready
    if _schema_ready:
        return True
    client = await get_client()
    if client is None:
        return False

    settings = get_settings()
    name = settings.weaviate_collection
    try:
        from weaviate.classes.config import (
            Configure,
            DataType,
            Property,
            VectorDistances,
        )

        if await client.collections.exists(name):
            _schema_ready = True
            return True

        await client.collections.create(
            name,
            properties=[
                Property(name="faq_id", data_type=DataType.INT),
                Property(name="lang", data_type=DataType.TEXT),
                Property(name="question", data_type=DataType.TEXT),
                Property(name="answer", data_type=DataType.TEXT),
            ],
            vector_config=[
                Configure.Vectors.text2vec_openai(
                    name="q",
                    source_properties=["question"],
                    model=settings.faq_embedding_model,
                    vector_index_config=Configure.VectorIndex.hnsw(
                        distance_metric=VectorDistances.COSINE,
                    ),
                )
            ],
        )
        logger.info("weaviate collection %s created", name)
        _schema_ready = True
        return True
    except Exception:
        logger.warning("weaviate ensure_schema failed", exc_info=True)
        return False


def _uuid(faq_id: int, lang: str) -> str:
    """Deterministic id so writes are upserts and deletes need no lookup."""
    from weaviate.util import generate_uuid5

    return generate_uuid5(f"{faq_id}:{lang}")


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

async def search(
    query: str,
    language: str | None = None,
    limit: int | None = None,
) -> list[FaqHit]:
    """Hybrid (BM25 + vector) search, best first.

    The embedding model is multilingual, so a non-ru query also searches the
    ru rows — entries without a translation stay findable, exactly as the old
    per-language UNION did.

    ``score`` is Weaviate's fused hybrid score. It ranks well but is NOT
    comparable across queries, so it must not be thresholded — the LLM rerank
    in ``faq_tools`` is what decides confidence.
    """
    if not (query or "").strip():
        return []
    settings = get_settings()
    if not await ensure_schema():
        return []
    client = await get_client()
    if client is None:
        return []

    lang = language if language in LANGS else "ru"
    langs = [lang] if lang == "ru" else [lang, "ru"]

    try:
        from weaviate.classes.query import Filter, MetadataQuery

        col = client.collections.use(settings.weaviate_collection)
        res = await col.query.hybrid(
            query=query,
            alpha=settings.faq_hybrid_alpha,
            limit=limit or settings.faq_candidate_limit,
            # BM25 leg reads both fields; the question is the stronger signal.
            query_properties=["question^2", "answer"],
            filters=Filter.by_property("lang").contains_any(langs),
            return_metadata=MetadataQuery(score=True),
        )
    except Exception:
        logger.warning("weaviate hybrid search failed", exc_info=True)
        return []

    hits: list[FaqHit] = []
    seen: set[str] = set()
    for obj in res.objects:
        p = obj.properties or {}
        answer = str(p.get("answer") or "")
        if not answer or answer in seen:
            # The same row can surface via both the language and the ru leg.
            continue
        seen.add(answer)
        hits.append(
            FaqHit(
                faq_id=int(p.get("faq_id") or 0),
                lang=str(p.get("lang") or ""),
                question=str(p.get("question") or ""),
                answer=answer,
                score=float(getattr(obj.metadata, "score", 0.0) or 0.0),
            )
        )
    return hits


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------

def _rows_for(faq_id: int, per_lang: dict[str, tuple[str | None, str | None]]) -> list[dict]:
    """Build the per-language objects for one FAQ row, skipping empty ones."""
    out = []
    for lang in LANGS:
        question, answer = per_lang.get(lang, (None, None))
        if not (question and answer):
            continue
        out.append(
            {
                "uuid": _uuid(faq_id, lang),
                "properties": {
                    "faq_id": int(faq_id),
                    "lang": lang,
                    "question": str(question),
                    "answer": str(answer),
                },
            }
        )
    return out


async def upsert(faq_id: int, per_lang: dict[str, tuple[str | None, str | None]]) -> bool:
    """Insert-or-replace every language variant of one FAQ row.

    ``per_lang`` maps ``"ru"|"en"|"uz"`` to ``(question, answer)``. A language
    whose question or answer is empty is deleted from the index rather than
    written, so clearing a translation removes it from search.
    """
    if not await ensure_schema():
        return False
    client = await get_client()
    if client is None:
        return False

    settings = get_settings()
    rows = _rows_for(faq_id, per_lang)
    present = {r["properties"]["lang"] for r in rows}
    try:
        col = client.collections.use(settings.weaviate_collection)
        for row in rows:
            await col.data.delete_by_id(row["uuid"])
            await col.data.insert(properties=row["properties"], uuid=row["uuid"])
        for lang in LANGS:
            if lang not in present:
                await col.data.delete_by_id(_uuid(faq_id, lang))
        return True
    except Exception:
        logger.warning("weaviate upsert failed for faq_id=%s", faq_id, exc_info=True)
        return False


async def delete(faq_id: int) -> bool:
    """Remove every language variant of one FAQ row from the index."""
    if not await ensure_schema():
        return False
    client = await get_client()
    if client is None:
        return False
    try:
        col = client.collections.use(get_settings().weaviate_collection)
        for lang in LANGS:
            await col.data.delete_by_id(_uuid(faq_id, lang))
        return True
    except Exception:
        logger.warning("weaviate delete failed for faq_id=%s", faq_id, exc_info=True)
        return False


async def reindex(rows: Iterable[tuple[int, dict[str, tuple[str | None, str | None]]]]) -> dict[str, int]:
    """Rebuild the whole collection from Postgres. Drives the admin button.

    Drops and recreates the collection so rows deleted in Postgres cannot
    linger in the index — this is the repair path for any drift between the
    two stores, and the reason writes elsewhere are allowed to fail softly.
    """
    global _schema_ready
    counts = {lang: 0 for lang in LANGS}
    counts["rows"] = 0
    client = await get_client()
    if client is None:
        return counts

    settings = get_settings()
    name = settings.weaviate_collection
    try:
        if await client.collections.exists(name):
            await client.collections.delete(name)
        _schema_ready = False
        if not await ensure_schema():
            return counts

        col = client.collections.use(name)
        batch: list[Any] = []
        from weaviate.classes.data import DataObject

        for faq_id, per_lang in rows:
            counts["rows"] += 1
            for row in _rows_for(faq_id, per_lang):
                batch.append(
                    DataObject(properties=row["properties"], uuid=row["uuid"])
                )
                counts[row["properties"]["lang"]] += 1
            if len(batch) >= 100:
                await col.data.insert_many(batch)
                batch = []
        if batch:
            await col.data.insert_many(batch)
    except Exception:
        logger.exception("weaviate reindex failed")
    return counts


# ---------------------------------------------------------------------------
# Synchronous facade — for SQLAlchemy event listeners
# ---------------------------------------------------------------------------
# FAQ writes happen inside a synchronous flush in the admin, where awaiting is
# awkward. These open a short-lived sync client per call: FAQ edits are rare
# (a handful per day), so the connection cost is irrelevant next to the
# complexity of bridging the async client into a sync context.


def _sync_client():
    settings = get_settings()
    if not settings.weaviate_enabled:
        return None
    try:
        import weaviate

        return weaviate.connect_to_custom(
            http_host=settings.weaviate_http_host,
            http_port=settings.weaviate_http_port,
            http_secure=settings.weaviate_secure,
            grpc_host=settings.weaviate_grpc_host,
            grpc_port=settings.weaviate_grpc_port,
            grpc_secure=settings.weaviate_secure,
            auth_credentials=_auth(),
            additional_config=_additional_config(),
            skip_init_checks=True,
        )
    except Exception:
        logger.warning("weaviate sync connect failed", exc_info=True)
        return None


def sync_upsert(faq_id: int, per_lang: dict[str, tuple[str | None, str | None]]) -> bool:
    """Synchronous :func:`upsert`, for ``app/db/events.py``."""
    client = _sync_client()
    if client is None:
        return False
    try:
        name = get_settings().weaviate_collection
        if not client.collections.exists(name):
            logger.warning("weaviate collection %s missing — run a reindex", name)
            return False
        col = client.collections.use(name)
        rows = _rows_for(faq_id, per_lang)
        present = {r["properties"]["lang"] for r in rows}
        for row in rows:
            col.data.delete_by_id(row["uuid"])
            col.data.insert(properties=row["properties"], uuid=row["uuid"])
        for lang in LANGS:
            if lang not in present:
                col.data.delete_by_id(_uuid(faq_id, lang))
        return True
    except Exception:
        logger.warning("weaviate sync upsert failed for faq_id=%s", faq_id, exc_info=True)
        return False
    finally:
        try:
            client.close()
        except Exception:
            pass


def sync_delete(faq_id: int) -> bool:
    """Synchronous :func:`delete`, for ``app/db/events.py``."""
    client = _sync_client()
    if client is None:
        return False
    try:
        name = get_settings().weaviate_collection
        if not client.collections.exists(name):
            return False
        col = client.collections.use(name)
        for lang in LANGS:
            col.data.delete_by_id(_uuid(faq_id, lang))
        return True
    except Exception:
        logger.warning("weaviate sync delete failed for faq_id=%s", faq_id, exc_info=True)
        return False
    finally:
        try:
            client.close()
        except Exception:
            pass
