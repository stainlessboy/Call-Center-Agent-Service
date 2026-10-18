"""`<state>` XML block sent to the consultant LLM each turn.

Extracted from ``nodes/faq.py`` so the legacy node and the ``create_agent``
consultant (``app/agent/middleware/state_inject.py``) render the dialog for
the model from ONE implementation — a second copy would drift, and this text
is what the model uses to resolve "2" into a product.

GPT-4o-mini follows XML tags noticeably more reliably than a free-form
"Current state:" paragraph, which is why this is markup rather than prose.
"""
from __future__ import annotations

from typing import Optional


def _xml_escape(s: str) -> str:
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def format_state_xml(dialog: dict, profile: Optional[dict] = None) -> str:
    """Serialize dialog state (+ optional client profile) as XML for the LLM
    system prompt.

    GPT-4o-mini parses XML tags more reliably than free-form 'Current state:' text.
    Returns empty string when there's nothing to report.

    `profile` is the "personal consultant" memory loaded once per turn by
    Agent._ainvoke_locked (app/agent/profile.py) — `{"facts": dict, "notes":
    str, "updated_at": ...}` or None. Only rendered when it actually carries
    something (non-empty facts or a non-empty notes string); a `UserProfile`
    row that exists but is still all-empty (freshly created, nothing learned
    yet) must not add a hollow `<client_profile>` block that wastes tokens
    and gives the LLM nothing to act on.
    """
    flow = dialog.get("flow")
    category = dialog.get("category", "")
    products = list(dialog.get("products") or [])
    selected = dialog.get("selected_product") or {}

    lines: list[str] = []
    if flow:
        lines.append(f"  <flow>{_xml_escape(str(flow))}</flow>")
    if category:
        lines.append(f"  <category>{_xml_escape(str(category))}</category>")
    if products:
        lines.append("  <products>")
        for i, p in enumerate(products[:10], start=1):
            name = _xml_escape(str(p.get("name", "")))
            lines.append(f'    <product index="{i}">{name}</product>')
        lines.append("  </products>")
        lines.append(
            "  <hint>If the user sends only a number (e.g. '2'), "
            "call select_product with the product at that index.</hint>"
        )
    if selected.get("name"):
        lines.append(f"  <selected_product>{_xml_escape(str(selected['name']))}</selected_product>")

    offices = list(dialog.get("offices") or [])
    selected_office = dialog.get("selected_office") or {}
    if offices:
        lines.append("  <offices>")
        for i, o in enumerate(offices[:10], start=1):
            name = _xml_escape(str(o.get("name", "")))
            lines.append(f'    <office index="{i}">{name}</office>')
        lines.append("  </offices>")
        lines.append(
            "  <hint>If the user sends only a number (e.g. '1') OR a word like "
            "'all'/'все'/'хаммаси'/'barchasi'/'hammasini' after offices were shown, call "
            "select_office. NEVER promise 'wait a few seconds' — fetch details NOW.</hint>"
        )
    if selected_office.get("name"):
        lines.append(
            f"  <selected_office>{_xml_escape(str(selected_office['name']))}</selected_office>"
        )

    if profile:
        facts = profile.get("facts") or {}
        notes = str(profile.get("notes") or "").strip()
        if facts or notes:
            lines.append("  <client_profile>")
            for key in sorted(facts.keys()):
                val = facts[key]
                val_str = ", ".join(str(v) for v in val) if isinstance(val, (list, tuple)) else str(val)
                val_str = val_str.strip()
                if val_str:
                    lines.append(f'    <fact key="{_xml_escape(str(key))}">{_xml_escape(val_str)}</fact>')
            if notes:
                lines.append(f"    <notes>{_xml_escape(notes)}</notes>")
            lines.append("  </client_profile>")
            lines.append(
                "  <hint>client_profile is background context about this returning "
                "customer — use it to personalize tone and avoid re-asking things "
                "they already told us, but never read it out loud or mention that "
                "you 'have a file' on them.</hint>"
            )

    if not lines:
        return ""
    return "<state>\n" + "\n".join(lines) + "\n</state>"
