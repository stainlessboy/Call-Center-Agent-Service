"""Natural-language rephrase pass for verified FAQ DB answers.

Two call sites return a raw FAQ answer straight from the `faq` table:
`nodes/faq.py::node_faq`'s deterministic strict pre-check (skips the LLM
entirely), and the `faq_lookup` tool's own "confident match" / low-confidence
"pick a candidate" paths (the LLM stays in the loop there, but the system
policy — see i18n.py "РАБОТА С FAQ" — instructs it to rephrase rather than
recite the DB text verbatim). This module covers the FIRST case: there is no
LLM turn to do the rephrasing, so we run one ourselves.

Read to a customer verbatim, DB text can sound stiff/copy-pasted — but it's
also the single source of truth for facts (rates, terms, procedures), so it
must never be *changed* by the rephrase, only reworded. `rephrase_faq_answer`
runs ONE tight, tool-free LLM call and then applies a deterministic safety
guard (`_passes_safety_guard`) that discards the rephrase — falling back to
the untouched source — the moment it can't prove every number and URL from
the source survived the rewrite, or the result looks empty/suspiciously
short/long. Same fallback on ANY exception, timeout, or disabled setting
(`FAQ_REPHRASE_ENABLED`). This function must never raise and must never fail
a turn — worst case it silently returns the source unchanged.
"""
from __future__ import annotations

import asyncio
import logging as _logging
import re

from langchain_core.messages import HumanMessage, SystemMessage

from app.agent.llm import _get_chat_openai, extract_text_content
from app.config import get_settings

_logger = _logging.getLogger(__name__)

_REPHRASE_TIMEOUT_SECONDS = 8.0

# Tool-free, history-free — a single tight instruction turn. Kept in Russian
# (the operator-facing prompts in this codebase — see handoff.py — are also
# Russian-only) since it targets an internal instruction-following task, not
# a customer-facing reply; the {lang} placeholder still forces the OUTPUT
# into the customer's language regardless of the prompt's own language.
_SYSTEM_PROMPT = """Ты помогаешь банковскому консультанту переформулировать проверенный ответ \
из базы знаний банка более естественным, разговорным языком — как будто отвечает живой человек, \
а не зачитывает документ.

СТРОГИЕ ПРАВИЛА:
- Сохрани АБСОЛЮТНО ВСЕ факты, числа, ставки, сроки, условия, названия и ссылки из исходного \
текста ТОЧНО как есть — ни одна цифра, дата, процент, срок или ссылка не должны измениться, \
пропасть или появиться новые.
- НЕ добавляй ничего, чего нет в исходном тексте — ни банковских деталей, ни оговорок, ни \
предположений.
- НЕ добавляй дисклеймеры вроде "это общая информация" — система сама оборачивает ответ при \
необходимости.
- Отвечай ТОЛЬКО на языке клиента (код языка: {lang}), даже если исходный текст на другом языке.
- Будь кратким — переформулировка не должна быть намного длиннее исходного текста.
- Верни ТОЛЬКО переформулированный текст ответа, без вступлений и пояснений от себя.

Вопрос клиента: {question}

Исходный текст ответа (источник истины, из базы знаний банка):
{source}"""

_HUMAN_PROMPT = "Переформулируй ответ по инструкции выше."


def _extract_numbers(text: str) -> set[str]:
    """Numeric tokens in *text*, robust to space-grouped thousands
    ("1 000 000" and "1000000" both yield the token "1000000") without
    merging genuinely distinct numbers — a comma, word, or symbol between
    two digits still breaks the run; only a bare space directly between two
    digits is treated as a thousands separator."""
    collapsed = text
    for _ in range(4):  # a few passes fold multi-group numbers like "1 000 000"
        merged = re.sub(r"(?<=\d)[  ](?=\d)", "", collapsed)
        if merged == collapsed:
            break
        collapsed = merged
    return set(re.findall(r"\d+(?:[.,]\d+)?", collapsed))


_URL_RE = re.compile(r"https?://\S+|www\.\S+", re.IGNORECASE)


def _extract_urls(text: str) -> set[str]:
    return set(_URL_RE.findall(text))


def _passes_safety_guard(source: str, rephrased: str) -> bool:
    """Deterministic, non-LLM check: the rephrase must carry every number and
    URL that appears in the source, and be a plausible length. Conservative
    on purpose — a false negative (discarding a perfectly fine rephrase)
    just costs a slightly stiffer reply; a false positive (accepting a
    rephrase that silently dropped a rate, a deadline, or a document
    requirement) would ship wrong banking facts to a customer.
    """
    if not rephrased or not rephrased.strip():
        return False
    if not _extract_numbers(source) <= _extract_numbers(rephrased):
        return False
    if not _extract_urls(source) <= _extract_urls(rephrased):
        return False
    src_len = len(source)
    out_len = len(rephrased)
    if src_len and not (src_len * 0.3 <= out_len <= src_len * 2.5):
        return False
    return True


async def rephrase_faq_answer(source_answer: str, user_question: str, lang: str) -> str:
    """Return a naturally-worded version of *source_answer* in *lang*.

    Falls back to the verbatim *source_answer* when: rephrasing is disabled
    (`FAQ_REPHRASE_ENABLED=false`), the LLM is unavailable, the call raises
    or times out, or the result fails `_passes_safety_guard`. Never raises.
    """
    if not source_answer or not source_answer.strip():
        return source_answer
    try:
        if not get_settings().faq_rephrase_enabled:
            return source_answer
        llm = _get_chat_openai(role="consultant")
        if llm is None:
            return source_answer
        prompt = _SYSTEM_PROMPT.format(
            lang=lang or "ru", question=user_question or "", source=source_answer,
        )
        ai_msg = await asyncio.wait_for(
            llm.ainvoke([SystemMessage(content=prompt), HumanMessage(content=_HUMAN_PROMPT)]),
            timeout=_REPHRASE_TIMEOUT_SECONDS,
        )
        rephrased = extract_text_content(ai_msg).strip()
        if _passes_safety_guard(source_answer, rephrased):
            return rephrased
        _logger.info("faq_rephrase: safety guard rejected the rephrase, using source verbatim")
        return source_answer
    except Exception:
        _logger.warning("faq_rephrase: rephrase failed, using source verbatim", exc_info=True)
        return source_answer
