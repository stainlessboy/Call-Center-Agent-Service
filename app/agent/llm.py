from __future__ import annotations

import os
from functools import lru_cache
from typing import Any, Optional

from langchain_openai import ChatOpenAI


@lru_cache(maxsize=1)
def _get_chat_openai() -> Optional[ChatOpenAI]:
    """Return a LangChain ChatOpenAI instance."""
    try:
        kwargs: dict[str, Any] = {
            "model": os.getenv("LOCAL_AGENT_INTENT_LLM_MODEL", "gpt-4o-mini"),
            "temperature": 0.3,
            "max_tokens": 512,
            "api_key": os.getenv("OPENAI_API_KEY"),
        }
        base_url = os.getenv("OPENAI_BASE_URL")
        if base_url:
            kwargs["base_url"] = base_url
        return ChatOpenAI(**kwargs)
    except Exception:
        return None
