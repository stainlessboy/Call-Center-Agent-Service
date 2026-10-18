"""Reword a verified FAQ answer before it ships (roadmap П.2).

`faq_lookup` returns one of three things (see the tool's docstring):

  * a STRICT-tier answer — verbatim text out of the `faq` table;
  * a `FAQ_LOW_CONFIDENCE` sentinel plus candidates;
  * `NO_MATCH_IN_FAQ`.

Only the first case needs handling here. In the legacy node it was surfaced
to the customer directly by the display-tool short-circuit, which skipped the
wrapping LLM turn — so nothing was left to apply the "rephrase, don't recite"
policy, and the raw database row shipped as-is. `faq_rephrase.rephrase_faq_answer`
exists for exactly that path: one tight LLM call, guarded so a rewrite that
drops a number or a URL is discarded in favour of the verbatim text.

The sentinels deliberately fall through untouched: they are instructions to
the model, not text for the customer, and the model composes the reply itself.
"""
from __future__ import annotations

import logging as _logging

from langchain_core.messages import ToolMessage

from app.agent.faq_rephrase import rephrase_faq_answer
from app.agent.middleware.common import lang_of
from app.agent.tools import is_faq_sentinel

_logger = _logging.getLogger(__name__)


async def rephrase_faq_tool_result(request, handler):
    """`wrap_tool_call` body (async): rephrase a strict `faq_lookup` result."""
    result = await handler(request)

    if (request.tool_call or {}).get("name") != "faq_lookup":
        return result
    if not isinstance(result, ToolMessage) or getattr(result, "status", None) == "error":
        return result

    content = str(result.content or "").strip()
    if not content or is_faq_sentinel(content):
        return result

    state = getattr(request, "state", None) or {}
    # The user's question, for context — the tool call's own argument is the
    # cleaned query the model chose, which is what the rephraser wants.
    query = str((request.tool_call or {}).get("args", {}).get("query") or "")
    try:
        reworded = await rephrase_faq_answer(content, query, lang_of(state))
    except Exception as exc:  # pragma: no cover - defensive
        _logger.debug("FAQ rephrase failed, shipping verbatim: %s", exc)
        return result

    if not reworded or reworded == content:
        return result
    return ToolMessage(
        content=reworded,
        tool_call_id=result.tool_call_id,
        name=result.name,
        artifact=getattr(result, "artifact", None),
    )
