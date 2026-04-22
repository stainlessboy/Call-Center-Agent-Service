from __future__ import annotations

import os
from functools import lru_cache
from typing import Any, Optional

from langchain_openai import ChatOpenAI

# Pricing per 1M tokens (input / output) — update when models change
_MODEL_PRICING: dict[str, tuple[float, float]] = {
    # GPT-4o family
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4o-mini-2024-07-18": (0.15, 0.60),
    "gpt-4o": (2.50, 10.00),
    "gpt-4o-2024-08-06": (2.50, 10.00),
    # GPT-4.1 family
    "gpt-4.1-mini": (0.40, 1.60),
    "gpt-4.1-nano": (0.10, 0.40),
    "gpt-4.1": (2.00, 8.00),
    # GPT-5 family
    "gpt-5": (1.25, 10.00),
    "gpt-5-mini": (0.25, 2.00),
    "gpt-5-nano": (0.05, 0.40),
    # GPT-5.4 family (released 2026-03-17)
    "gpt-5.4": (2.50, 15.00),
    "gpt-5.4-mini": (0.75, 4.50),
    "gpt-5.4-nano": (0.20, 1.25),
    "gpt-5.4-pro": (30.00, 180.00),
}


def _is_reasoning_model(model_name: str) -> bool:
    """True if the model accepts the `reasoning_effort` parameter (GPT-5 / o-series)."""
    return model_name.startswith(("gpt-5", "o1", "o3", "o4"))


def _default_reasoning_effort(model_name: str) -> str:
    """Cheapest/fastest effort value accepted by the given model family.

    The allowed values differ between families:
      * gpt-5.x (5.4, 5.5, ...)  → {none, low, medium, high, xhigh}
      * gpt-5 / o-series         → {minimal, low, medium, high}
    """
    if model_name.startswith("gpt-5."):
        return "none"
    return "minimal"


def _needs_responses_api(model_name: str) -> bool:
    """gpt-5.x family rejects `reasoning_effort` + function tools on
    /v1/chat/completions — only the /v1/responses endpoint supports that
    combination. Our bot always binds tools in node_faq, so we must route
    those models through responses API."""
    return model_name.startswith("gpt-5.")


@lru_cache(maxsize=1)
def _get_chat_openai() -> Optional[ChatOpenAI]:
    """Return a LangChain ChatOpenAI instance."""
    try:
        model_name = (
            os.getenv("OPENAI_MODEL")
            or os.getenv("LOCAL_AGENT_INTENT_LLM_MODEL")
            or "gpt-4o-mini"
        )
        kwargs: dict[str, Any] = {
            "model": model_name,
            "temperature": 0.3,
            "max_tokens": 512,
            "api_key": os.getenv("OPENAI_API_KEY"),
        }
        base_url = os.getenv("OPENAI_BASE_URL")
        if base_url:
            kwargs["base_url"] = base_url
        # Reasoning-capable models (GPT-5 family, o-series) charge for hidden
        # reasoning tokens. For chat/tool-calling use cases the reasoning phase
        # is wasteful — pick the cheapest/fastest effort per family.
        # Override via REASONING_EFFORT env.
        if _is_reasoning_model(model_name):
            kwargs["reasoning_effort"] = (
                os.getenv("REASONING_EFFORT") or _default_reasoning_effort(model_name)
            )
        # gpt-5.x must use /v1/responses endpoint when combining tools with
        # reasoning_effort. ChatOpenAI with use_responses_api=True handles this.
        if _needs_responses_api(model_name):
            kwargs["use_responses_api"] = True
        return ChatOpenAI(**kwargs)
    except Exception as exc:
        import logging
        logging.getLogger(__name__).warning("Failed to create ChatOpenAI: %s", exc)
        return None


def get_model_name() -> str:
    return os.getenv("OPENAI_MODEL") or os.getenv("LOCAL_AGENT_INTENT_LLM_MODEL") or "gpt-4o-mini"


def extract_text_content(ai_msg: Any) -> str:
    """Extract plain text from an AIMessage regardless of API shape.

    `/v1/responses` (used for gpt-5.x) returns content as a list of blocks:
        [{"type": "text", "text": "...", ...}, ...]
    while `/v1/chat/completions` returns content as a plain string.
    """
    content = getattr(ai_msg, "content", None)
    if not content:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                txt = block.get("text")
                if txt:
                    parts.append(str(txt))
            elif isinstance(block, str):
                parts.append(block)
        return "".join(parts)
    return str(content)


def extract_token_usage(ai_msg: Any) -> dict:
    """Extract token usage from a LangChain AIMessage.

    Handles three shapes:
    1. `AIMessage.usage_metadata` (LangChain's normalised form — preferred)
       {"input_tokens": N, "output_tokens": M, "total_tokens": T, ...}
    2. `response_metadata["token_usage"]` (/v1/chat/completions)
       {"prompt_tokens": N, "completion_tokens": M, "total_tokens": T}
    3. `response_metadata["usage"]` (/v1/responses)
       {"input_tokens": N, "output_tokens": M, "total_tokens": T,
        "output_tokens_details": {"reasoning_tokens": R}}

    Returns normalised dict with "prompt_tokens" / "completion_tokens"
    (the names expected by calculate_cost), plus "total_tokens".
    """
    # 1) Preferred: usage_metadata (langchain-core ≥0.2 sets this)
    um = getattr(ai_msg, "usage_metadata", None) or {}
    if um:
        return {
            "prompt_tokens": int(um.get("input_tokens") or 0),
            "completion_tokens": int(um.get("output_tokens") or 0),
            "total_tokens": int(um.get("total_tokens") or 0),
        }

    # 2) Fallback: response_metadata
    meta = getattr(ai_msg, "response_metadata", None) or {}
    tu = meta.get("token_usage") or meta.get("usage") or {}
    if not tu:
        return {}

    prompt = tu.get("prompt_tokens") or tu.get("input_tokens") or 0
    completion = tu.get("completion_tokens") or tu.get("output_tokens") or 0
    total = tu.get("total_tokens") or (int(prompt) + int(completion))
    return {
        "prompt_tokens": int(prompt),
        "completion_tokens": int(completion),
        "total_tokens": int(total),
    }


def accumulate_usage(totals: dict, new_usage: dict) -> dict:
    """Add new_usage into totals dict, accumulating token counts."""
    if not new_usage:
        return totals
    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
        totals[key] = totals.get(key, 0) + new_usage.get(key, 0)
    return totals


def calculate_cost(usage: dict, model: Optional[str] = None) -> float:
    """Calculate cost in USD from token usage."""
    model = model or get_model_name()
    pricing = _MODEL_PRICING.get(model)
    if not pricing or not usage:
        return 0.0
    input_price, output_price = pricing
    prompt = usage.get("prompt_tokens", 0)
    completion = usage.get("completion_tokens", 0)
    return (prompt * input_price + completion * output_price) / 1_000_000


def finalize_usage(usage: dict, model: Optional[str] = None) -> dict:
    """Add cost and model to usage dict. Returns the same dict mutated."""
    if not usage:
        return usage
    model = model or get_model_name()
    usage["cost"] = calculate_cost(usage, model)
    usage["model"] = model
    return usage
