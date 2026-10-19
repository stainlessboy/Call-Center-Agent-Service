"""Recover a tool call the model meant to make but wrote as plain text.

Small quantized models (measured: Gemma 4 26B-A4B QAT Q4_0 on llama-server,
RTX 4080, 2026-10-18) sometimes answer a product request without a real
``tool_calls`` entry. Three shapes were captured in the raw output of
``scripts/diag_model.py --dump``:

* the call written as markup text — ``<get_products category="autoloan"/>``
  or ``<call:get_products(category="autoloan")/>``. The model's native call
  is ``<|tool_call>call:NAME{...}<tool_call|>`` built from special tokens;
  these are plain-text imitations, so no server parser recognises them;
* reasoning leaked into the answer despite ``enable_thinking: false`` —
  ``<thought`` + "Thinking Process: ..." that usually names the right call
  ("Action: Call `get_products(category="deposit")`") but never makes it;
* a free-text reply to a clear product request ("Karta ochmoqchiman"): a
  clarifying question or a menu the model made up, instead of the catalog —
  or to an explicit operator request ("Hozir sizni operatorga ulayman" with no
  call, so the promised handoff never happened).

``rescue_tool_call`` turns the first two into the call they describe and, for
the third, falls back to the rule-based detectors in ``app/agent/intent.py``. ``node_faq`` uses
it only when the model returned no tool call at all, so a turn where the model
chose a tool itself is never second-guessed.
"""
from __future__ import annotations

import re
from typing import Iterable, Optional

from app.agent.intent import _intent_tool_call

# "<thought", "Thinking Process:" — the leaked reasoning preamble.
_THOUGHT_LEAK_RE = re.compile(r"^\s*(<\|?thought\b|<\|channel>thought|thinking process\s*:)", re.IGNORECASE)

# One `key=value` / `key: value` pair inside the call text. Values may be
# double/single quoted, wrapped in Gemma's <|"|> escape token, or bare.
_ARG_RE = re.compile(
    r"""(\w+)\s*[=:]\s*(?:"([^"]*)"|'([^']*)'|<\|"\|>(.*?)<\|"\|>|([^,\s)}/>]+))"""
)


def looks_like_thought_leak(content: str) -> bool:
    """True if the reply opens with leaked reasoning instead of an answer."""
    return bool(_THOUGHT_LEAK_RE.match(content or ""))


def _coerce(value: str):
    """Numbers stay numbers (calculator tools take amounts/terms)."""
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        return value


def _parse_args(text: str) -> dict:
    args: dict = {}
    for m in _ARG_RE.finditer(text or ""):
        key = m.group(1)
        raw = next((g for g in m.groups()[1:] if g is not None), "")
        args[key] = _coerce(raw)
    return args


def salvage_tool_call(content: str, tool_names: Iterable[str]) -> Optional[tuple[str, dict]]:
    """Find a call to a known tool written as text; return (name, args) or None.

    Only names from *tool_names* are accepted, so ordinary prose that happens
    to contain parentheses can never become a call. When several candidates
    appear (leaked reasoning often names the tool more than once), the last
    one wins — it is the model's final decision.
    """
    if not content:
        return None
    names = sorted({n for n in tool_names if n}, key=len, reverse=True)
    if not names:
        return None
    alt = "|".join(re.escape(n) for n in names)
    patterns = (
        # <call:get_products(category="autoloan")/>, call:get_products{...}
        rf"call:\s*({alt})\s*[({{]([^)}}]*)[)}}]",
        # <get_products category="autoloan"/>
        rf"<\s*({alt})\b([^>]*?)/?>",
        # get_products(category="deposit") inside prose / leaked reasoning
        rf"\b({alt})\s*\(([^)]*)\)",
    )
    found: list[tuple[int, str, str]] = []
    for pat in patterns:
        for m in re.finditer(pat, content):
            found.append((m.start(), m.group(1), m.group(2)))
    if not found:
        return None
    _, name, arg_text = max(found, key=lambda f: f[0])
    return name, _parse_args(arg_text)


def rescue_tool_call(
    content: str,
    user_text: str,
    tool_names: Iterable[str],
    *,
    allow_intent: bool,
) -> Optional[tuple[str, dict, str]]:
    """Decide which tool call a text-only reply should have been.

    Returns ``(name, args, reason)`` or ``None``. ``reason`` is
    ``"call_as_text"`` (the reply spelled a call out), or ``"intent"`` (the
    user's message is a clear product request and the reply was not a call —
    only when *allow_intent*, i.e. on the turn's first model decision before
    any tool ran, because after a tool result a text answer is legitimate).
    """
    names = list(tool_names)
    salvaged = salvage_tool_call(content, names)
    if salvaged is not None:
        return salvaged[0], salvaged[1], "call_as_text"
    if allow_intent:
        intent = _intent_tool_call(user_text)
        if intent is not None and intent[0] in names:
            return intent[0], intent[1], "intent"
    return None
