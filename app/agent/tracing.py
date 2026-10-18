"""LangSmith tracing for the agent turn (roadmap П.7а).

Why this module exists instead of just setting ``LANGSMITH_TRACING=true``
-----------------------------------------------------------------------
The stock env switch traces every run with LangSmith's *default* client,
which uploads node inputs and outputs verbatim. Our graph state legitimately
carries raw PII that must NOT leave the process:

  * ``state.last_user_text`` — the raw customer message. It cannot be masked
    in the state itself: ``node_calc_flow``'s lead-capture step parses the
    customer's real phone number and name out of exactly this field, so a
    masked state would break lead capture (that is also why
    ``_finalize_turn`` masks only the copy it appends to ``messages``).
  * ``dialog.lead_slots`` — the captured name and phone, by definition.

So instead of the global switch we build our own ``LangChainTracer`` whose
``Client`` is configured with ``hide_inputs`` / ``hide_outputs`` callables
that run every string through the project's existing ``mask_pii`` regexes
before anything is uploaded. The tracer is attached per-run via
``config["callbacks"]``.

Enable with ``AGENT_TRACING_ENABLED=true`` (plus ``LANGSMITH_API_KEY``).
Do NOT also set ``LANGSMITH_TRACING=true``: that would attach LangChain's
own unmasked tracer on top of ours, producing duplicate traces AND leaking
the very PII this module masks. That combination is detected and refused
below, with a warning naming the risk.
"""
from __future__ import annotations

import logging as _logging
import os
from functools import lru_cache
from typing import Any, Optional, Sequence

from app.agent.pii_masker import mask_pii

_logger = _logging.getLogger(__name__)

# Depth/'breadth' caps for the masking walk. Graph state is shallow (a dict of
# scalars plus `dialog`, `messages`, `user_profile`), so these only exist so a
# pathological payload can never make masking the slowest part of a turn.
_MAX_DEPTH = 6
_MAX_ITEMS = 500
# Strings longer than this are truncated before masking — a giant paste is
# never worth uploading in full anyway.
_MAX_STR = 20_000


def _env_truthy(value: Optional[str]) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def tracing_requested() -> bool:
    """True when AGENT_TRACING_ENABLED asks for masked LangSmith tracing."""
    return _env_truthy(os.getenv("AGENT_TRACING_ENABLED"))


def _mask_value(value: Any, depth: int = 0) -> Any:
    """Recursively mask PII in an arbitrary JSON-ish payload.

    Strings go through ``mask_pii``; containers are walked; everything else
    (numbers, bools, None) is returned untouched. Anything deeper than
    ``_MAX_DEPTH`` — or an object we don't recognise — is replaced by a type
    marker rather than uploaded blind, because an unrecognised object could
    stringify to anything, including PII.
    """
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return mask_pii(value[:_MAX_STR])
    if depth >= _MAX_DEPTH:
        return f"<{type(value).__name__}>"
    if isinstance(value, dict):
        out: dict = {}
        for i, (key, val) in enumerate(value.items()):
            if i >= _MAX_ITEMS:
                out["…"] = f"<{len(value) - _MAX_ITEMS} more keys>"
                break
            out[str(key)] = _mask_value(val, depth + 1)
        return out
    if isinstance(value, (list, tuple, set)):
        items = list(value)[:_MAX_ITEMS]
        return [_mask_value(v, depth + 1) for v in items]
    # LangChain messages and other objects: mask their text content if we can
    # reach it, otherwise fall back to a type marker.
    content = getattr(value, "content", None)
    if content is not None:
        return {
            "type": type(value).__name__,
            "content": _mask_value(content, depth + 1),
        }
    return f"<{type(value).__name__}>"


def _mask_payload(payload: dict) -> dict:
    """`hide_inputs` / `hide_outputs` hook. Must never raise: a masking
    failure would otherwise kill the run it is tracing."""
    try:
        masked = _mask_value(payload)
        return masked if isinstance(masked, dict) else {"value": masked}
    except Exception as exc:  # pragma: no cover - defensive
        _logger.warning("Trace masking failed, dropping payload: %s", exc)
        return {"masked": "<masking failed>"}


@lru_cache(maxsize=1)
def get_tracer():
    """Build (once) the masked LangChainTracer, or return None.

    Returns None — tracing simply off — when: the feature flag is unset, no
    API key is configured, the stock ``LANGSMITH_TRACING`` switch is also on
    (see module docstring), or construction fails for any reason. Tracing is
    an observability nicety and must never break a customer turn.
    """
    if not tracing_requested():
        return None
    if _env_truthy(os.getenv("LANGSMITH_TRACING")) or _env_truthy(os.getenv("LANGCHAIN_TRACING_V2")):
        _logger.warning(
            "AGENT_TRACING_ENABLED and LANGSMITH_TRACING are both set. Refusing to "
            "attach the PII-masking tracer, because LangChain's own global tracer "
            "is already active and would (a) duplicate every trace and (b) upload "
            "unmasked customer text. Unset LANGSMITH_TRACING to get masked traces."
        )
        return None
    if not os.getenv("LANGSMITH_API_KEY"):
        _logger.warning("AGENT_TRACING_ENABLED is set but LANGSMITH_API_KEY is missing — tracing off.")
        return None
    try:
        from langchain_core.tracers.langchain import LangChainTracer
        from langsmith import Client

        client = Client(
            api_key=os.getenv("LANGSMITH_API_KEY"),
            api_url=os.getenv("LANGSMITH_ENDPOINT") or None,
            hide_inputs=_mask_payload,
            hide_outputs=_mask_payload,
        )
        tracer = LangChainTracer(
            project_name=os.getenv("LANGSMITH_PROJECT") or "asaka-bot",
            client=client,
        )
        _logger.info(
            "LangSmith tracing enabled (project=%s, PII masking on)",
            os.getenv("LANGSMITH_PROJECT") or "asaka-bot",
        )
        return tracer
    except Exception as exc:
        _logger.warning("Failed to initialise LangSmith tracer: %s", exc)
        return None


def build_run_config(
    session_id: str,
    *,
    user_id: Optional[int] = None,
    lang: Optional[str] = None,
    channel: Optional[str] = None,
    flow: Optional[str] = None,
    run_name: Optional[str] = None,
    extra_metadata: Optional[dict] = None,
    extra_tags: Sequence[str] = (),
) -> dict:
    """Build the per-run LangGraph config.

    Always sets ``configurable.thread_id`` (the checkpointer key — this is
    what every call site needs regardless of tracing). Metadata and tags are
    attached unconditionally: they cost nothing when tracing is off and make
    a trace findable by ``session_id`` / ``user_id`` when it is on. The
    masking tracer is attached only when configured.
    """
    metadata: dict[str, Any] = {"session_id": session_id}
    if user_id is not None:
        metadata["user_id"] = user_id
    if lang:
        metadata["lang"] = lang
    if channel:
        metadata["channel"] = channel
    if flow:
        metadata["flow"] = flow
    if extra_metadata:
        metadata.update(extra_metadata)

    tags = [t for t in (channel, lang, flow, *extra_tags) if t]

    config: dict[str, Any] = {
        "configurable": {"thread_id": session_id},
        "metadata": metadata,
        "tags": tags,
    }
    if run_name:
        config["run_name"] = run_name

    tracer = get_tracer()
    if tracer is not None:
        config["callbacks"] = [tracer]
    return config
