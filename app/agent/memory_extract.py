"""Background "personal consultant" memory extractor.

After each agent turn, `Agent._ainvoke_locked` (agent.py) fires
`extract_and_store` as a fire-and-forget background task (not awaited — must
never add latency to the user-facing turn). It re-reads the just-finished
exchange with a cheap LLM call (role="extractor", see app/agent/llm.py) and
asks it for two things in one strict-JSON response:

  * NEW client facts not already in the stored profile (income, age,
    employment, family, goals, preferences — schema is loose, see
    UserProfile.facts in app/db/models.py).
  * An updated 2-4 sentence "relationship recap" (`notes`), or `null` if
    nothing about the relationship changed this turn.

An empty result (`{"facts": {}, "notes": null}`) is a normal, frequent, valid
outcome — most turns don't reveal anything new — and results in NO database
write (see app/agent/profile.py::upsert_user_profile's no-op short circuit).

Every failure mode here (LLM unavailable, timeout, malformed JSON, DB error)
is caught and logged — this function must NEVER raise into its caller's
fire-and-forget task, by design as much as by the caller's own done-callback
safety net.
"""
from __future__ import annotations

import asyncio
import json
import logging as _logging
from typing import Any, Optional

from langchain_core.messages import HumanMessage, SystemMessage

from app.agent.llm import _get_chat_openai, extract_text_content
from app.agent.pii_masker import mask_pii
from app.agent.profile import upsert_user_profile

_logger = _logging.getLogger(__name__)

_EXTRACT_TIMEOUT_SECONDS = 15.0
# Hard cap matching the spec's "максимум ~600 символов" — enforced here (not
# just requested in the prompt) so a verbose LLM response never blows past it.
_MAX_NOTES_CHARS = 600

# Single internal-Russian prompt — unlike the customer-facing consultant
# persona, nothing here is ever shown to the client: `notes` is read by
# node_faq (as `<client_profile>` context for the NEXT turn's LLM call) and,
# potentially, by RU-speaking bank staff. Localizing per dialog `lang` would
# only fragment the recap across languages turn to turn, so this is
# deliberately NOT keyed by lang (matching the function's signature, which
# has no `lang` parameter either).
_SYSTEM_PROMPT = """Ты — фоновый ассистент, который после каждого разговора между клиентом \
и банковским консультантом извлекает НОВЫЕ факты о клиенте и обновляет краткий конспект \
отношений с ним. Твой вывод никогда не показывается клиенту напрямую.

Уже известные факты о клиенте: {current_facts}
Текущий конспект отношений: {current_notes}

Реплика клиента: "{user_text}"
Ответ консультанта: "{answer}"

ЗАДАЧА 1 — facts (новые/изменившиеся факты):
Извлеки из ЭТОГО обмена факты о клиенте, которых ЕЩЁ НЕТ в списке известных фактов выше, \
или которые изменились по сравнению с известными. Если факт уже известен и не изменился — \
НЕ включай его повторно. Возможные ключи (используй только те, что явно следуют из текста):
- income_monthly: число — ежемесячный доход в сумах, если клиент явно его назвал
- age: число — полных лет
- income_type: СТРОГО одно из "payroll" (зарплата приходит на карту Asaka Bank) / \
"official" (официальная зарплата, но в другом банке) / "no_official" (самозанятый, без \
официального трудоустройства, пенсионер без офиц. дохода и т.п.) — включай, только если \
клиент явно и однозначно сообщил один из этих трёх вариантов
- family_status: строка — например "женат, двое детей"
- goals: список строк — финансовые/жизненные цели, например ["купить квартиру"]
- preferences: список строк — явные предпочтения, например ["предпочитает узбекский язык"]. \
Если клиент явно назвал предпочитаемую валюту для вклада/сбережений — ДОПОЛНИТЕЛЬНО добавь \
ключ currency: строго одно из "UZS"/"USD"/"EUR"
НИКОГДА не додумывай факт, если он не сказан явно текстом. Особенно строго отнесись к \
income_type и currency — это структурированные значения из фиксированного списка, ошибка \
здесь молча пропускает соответствующий вопрос анкеты клиенту.

ЗАДАЧА 2 — notes (конспект отношений):
2-4 предложения (не более {max_notes_chars} символов), кратко описывающие: кто этот клиент, \
какие продукты/темы обсуждались, на чём остановились, важные нюансы для следующего разговора. \
Если из ЭТОГО обмена нечего добавить или изменить в конспекте — верни notes: null (значит \
"оставить как есть"), не переписывай его без причины.

ЗАПРЕЩЕНО включать в facts или notes: ФИО, паспортные данные, номер карты/счёта, телефон, \
адрес, ПИНФЛ. Такие данные в тексте клиента уже заменены токенами вида [NAME]/[PHONE]/[CARD]/ \
[PASSPORT]/[PINFL]/[IBAN] — если встретишь такой токен, просто игнорируй его.

Если ни одного нового факта не найдено И конспект не требует обновления — это НОРМАЛЬНЫЙ и \
частый результат, верни {{"facts": {{}}, "notes": null}}.

Верни СТРОГО JSON, без markdown и пояснений:
{{"facts": {{<ключ>: <значение>, ...}} (или {{}}), "notes": "<текст>" (или null)}}
"""


def _build_prompt(current_profile: Optional[dict], user_text: str, answer: str) -> str:
    current_profile = current_profile or {}
    current_facts = current_profile.get("facts") or {}
    current_notes = current_profile.get("notes") or ""
    return _SYSTEM_PROMPT.format(
        current_facts=json.dumps(current_facts, ensure_ascii=False) if current_facts else "нет данных",
        current_notes=current_notes or "нет данных",
        user_text=user_text,
        answer=answer,
        max_notes_chars=_MAX_NOTES_CHARS,
    )


def _parse_extraction(raw: str) -> tuple[dict[str, Any], Optional[str]]:
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    result = json.loads(raw)
    facts = result.get("facts")
    facts = dict(facts) if isinstance(facts, dict) else {}
    notes = result.get("notes")
    if notes is not None:
        notes = str(notes).strip()[:_MAX_NOTES_CHARS] or None
    return facts, notes


async def extract_and_store(
    user_id: int,
    user_text: str,
    answer: str,
    current_profile: Optional[dict] = None,
) -> None:
    """Extract new client facts + an updated relationship recap, and store them.

    `user_id` is the Telegram user id (same convention as app/agent/profile.py —
    see that module's docstring for why it's not the internal `users.id` PK).
    `current_profile` is the profile as loaded at the START of this turn (see
    BotState.user_profile) — used as context so the LLM only reports what's
    NEW, and to avoid clobbering `notes` when there's nothing to add.
    """
    try:
        llm = _get_chat_openai(role="extractor")
        if llm is None:
            return
        masked_user_text = mask_pii(user_text or "")
        system_text = _build_prompt(current_profile, masked_user_text, answer or "")
        ai_msg = await asyncio.wait_for(
            llm.ainvoke([
                SystemMessage(content=system_text),
                HumanMessage(content="Извлеки факты и обнови конспект по инструкции выше."),
            ]),
            timeout=_EXTRACT_TIMEOUT_SECONDS,
        )
        raw = extract_text_content(ai_msg).strip()
        facts, notes = _parse_extraction(raw)
        if not facts and notes is None:
            # Valid, frequent outcome — nothing new this turn. No DB write.
            return
        await upsert_user_profile(user_id, facts_delta=facts, notes=notes)
    except Exception as exc:
        _logger.warning("Memory extraction failed for user %s: %s", user_id, exc)
