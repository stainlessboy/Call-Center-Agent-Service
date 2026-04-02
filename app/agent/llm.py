from __future__ import annotations

import os
from functools import lru_cache
from typing import Any, Optional

from langchain_openai import ChatOpenAI

# Pricing per 1M tokens (input / output) — update when models change
_MODEL_PRICING: dict[str, tuple[float, float]] = {
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4o-mini-2024-07-18": (0.15, 0.60),
    "gpt-4o": (2.50, 10.00),
    "gpt-4o-2024-08-06": (2.50, 10.00),
    "gpt-4.1-mini": (0.40, 1.60),
    "gpt-4.1-nano": (0.10, 0.40),
    "gpt-4.1": (2.00, 8.00),
}


@lru_cache(maxsize=1)
def _get_chat_openai() -> Optional[ChatOpenAI]:
    """Return a LangChain ChatOpenAI instance."""
    try:
        kwargs: dict[str, Any] = {
            "model": os.getenv("OPENAI_MODEL") or os.getenv("LOCAL_AGENT_INTENT_LLM_MODEL") or "gpt-4o-mini",
            "temperature": 0.3,
            "max_tokens": 512,
            "api_key": os.getenv("OPENAI_API_KEY"),
        }
        base_url = os.getenv("OPENAI_BASE_URL")
        if base_url:
            kwargs["base_url"] = base_url
        return ChatOpenAI(**kwargs)
    except Exception as exc:
        import logging
        logging.getLogger(__name__).warning("Failed to create ChatOpenAI: %s", exc)
        return None


def get_model_name() -> str:
    return os.getenv("OPENAI_MODEL") or os.getenv("LOCAL_AGENT_INTENT_LLM_MODEL") or "gpt-4o-mini"


def extract_token_usage(ai_msg: Any) -> dict:
    """Extract token usage from a LangChain AIMessage response_metadata."""
    usage: dict = {}
    meta = getattr(ai_msg, "response_metadata", None) or {}
    token_usage = meta.get("token_usage") or meta.get("usage") or {}
    if token_usage:
        usage["prompt_tokens"] = token_usage.get("prompt_tokens", 0)
        usage["completion_tokens"] = token_usage.get("completion_tokens", 0)
        usage["total_tokens"] = token_usage.get("total_tokens", 0)
    return usage


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
