"""Tests for the standalone HITL demo graph (examples/hitl_demo/graph.py).

These tests walk the ENTIRE scenario with scripted `Command(resume=...)`
calls and assert both the interrupt payloads surfaced at each pause and the
final state once the graph reaches `notify`/END. No network calls — the demo
graph never touches OpenAI unless explicitly built with `use_llm=True` AND
`OPENAI_API_KEY` is set (not the case here), so every node here runs the
deterministic template path.

Run with:  .venv/bin/python -m pytest tests/test_hitl_demo.py -v
"""

from __future__ import annotations

import uuid

import pytest
from langgraph.types import Command

from examples.hitl_demo import graph as graph_module
from examples.hitl_demo.graph import DEFAULT_DOCUMENTS, build_graph


@pytest.fixture(autouse=True)
def _clear_fake_crm():
    """`FAKE_CRM` is a module-level list standing in for a real database —
    clear it before every test so `application_id` numbering (APP-0001,
    APP-0002, ...) is deterministic and tests don't leak state into each
    other."""
    graph_module.FAKE_CRM.clear()
    yield
    graph_module.FAKE_CRM.clear()


@pytest.fixture()
def compiled_graph():
    """Fresh graph + fresh in-memory checkpointer per test."""
    return build_graph()


def _new_config() -> dict:
    return {"configurable": {"thread_id": str(uuid.uuid4())}}


def _interrupts(result: dict) -> list:
    return list(result.get("__interrupt__") or [])


def _approve_all_docs(graph, config, result: dict) -> dict:
    interrupts = _interrupts(result)
    assert {itr.value["type"] for itr in interrupts} == {"verify_document"}
    assert {itr.value["document"] for itr in interrupts} == set(DEFAULT_DOCUMENTS)
    resume_map = {itr.id: True for itr in interrupts}
    return graph.invoke(Command(resume=resume_map), config)


# --------------------------------------------------------------------------- #
# PATTERN 1 — ask the user for missing input
# --------------------------------------------------------------------------- #
def test_ask_user_fills_missing_fields_one_at_a_time(compiled_graph):
    config = _new_config()
    # No amount, no term, no purpose at all.
    result = compiled_graph.invoke({"customer_request": "Хочу кредит", "review_round": 0}, config)

    interrupts = _interrupts(result)
    assert len(interrupts) == 1
    payload = interrupts[0].value
    assert payload["type"] == "ask_user"
    assert payload["missing"] == ["amount", "term_months", "purpose"]
    assert "сумму" in payload["question"].lower()

    result = compiled_graph.invoke(Command(resume="20000000"), config)
    interrupts = _interrupts(result)
    assert len(interrupts) == 1
    payload = interrupts[0].value
    assert payload["missing"] == ["term_months", "purpose"]

    result = compiled_graph.invoke(Command(resume="24 месяца"), config)
    interrupts = _interrupts(result)
    assert len(interrupts) == 1
    payload = interrupts[0].value
    assert payload["missing"] == ["purpose"]

    result = compiled_graph.invoke(Command(resume="автокредит"), config)
    # All fields now known -> routed past intake into risk_check's parallel
    # document verification (PATTERN 2), proving the conditional-edge loop
    # back to `intake` terminates correctly.
    interrupts = _interrupts(result)
    assert {itr.value["type"] for itr in interrupts} == {"verify_document"}
    assert result["amount"] == 20_000_000
    assert result["term_months"] == 24
    assert result["purpose"] == "автокредит"


# --------------------------------------------------------------------------- #
# PATTERN 2 — parallel interrupts
# --------------------------------------------------------------------------- #
def test_parallel_document_verification_resumes_with_single_keyed_command(compiled_graph):
    config = _new_config()
    result = compiled_graph.invoke(
        {"customer_request": "Хочу автокредит на 50000000 сум на 36 месяцев", "review_round": 0},
        config,
    )
    interrupts = _interrupts(result)
    assert len(interrupts) == len(DEFAULT_DOCUMENTS)
    ids = {itr.id for itr in interrupts}
    assert len(ids) == len(interrupts), "each parallel interrupt must have a unique id"
    for itr in interrupts:
        assert itr.value == {"type": "verify_document", "document": itr.value["document"]}
        assert itr.value["document"] in DEFAULT_DOCUMENTS

    # Resume ALL three in ONE Command, keyed by interrupt id.
    resume_map = {itr.id: True for itr in interrupts}
    result = compiled_graph.invoke(Command(resume=resume_map), config)

    assert result["verified_docs"] == {doc: True for doc in DEFAULT_DOCUMENTS}
    # Low DTI for this amount/term -> risk should be low, so we proceed to
    # the offer/review pipeline (PATTERN 3) rather than operator_handoff.
    assert result["risk_level"] == "low"
    next_interrupts = _interrupts(result)
    assert next_interrupts[0].value["type"] == "review"


def test_unverified_document_routes_to_operator_handoff(compiled_graph):
    config = _new_config()
    result = compiled_graph.invoke(
        {"customer_request": "Хочу автокредит на 50000000 сум на 36 месяцев", "review_round": 0},
        config,
    )
    interrupts = _interrupts(result)
    resume_map = {itr.id: (itr.value["document"] != "ИНН") for itr in interrupts}
    result = compiled_graph.invoke(Command(resume=resume_map), config)

    assert result["verified_docs"]["ИНН"] is False
    interrupts = _interrupts(result)
    assert len(interrupts) == 1
    assert interrupts[0].value["type"] == "operator"
    assert "ИНН" in interrupts[0].value["summary"] or "документ" in interrupts[0].value["summary"].lower()

    result = compiled_graph.invoke(Command(resume="Пришлите ИНН повторно, перезвоним."), config)
    assert not _interrupts(result)
    assert result["operator_reply"] == "Пришлите ИНН повторно, перезвоним."
    assert result.get("application_id") is None
    assert "Ответ оператора" in result["final_message"]


# --------------------------------------------------------------------------- #
# PATTERN 3 — review & edit / reject with feedback
# --------------------------------------------------------------------------- #
def test_review_approve_flows_straight_through_to_submission(compiled_graph):
    config = _new_config()
    result = compiled_graph.invoke(
        {"customer_request": "Хочу автокредит на 50000000 сум на 36 месяцев", "review_round": 0},
        config,
    )
    result = _approve_all_docs(compiled_graph, config, result)

    review_interrupt = _interrupts(result)[0]
    assert review_interrupt.value["type"] == "review"
    assert review_interrupt.value["allowed_decisions"] == ["approve", "edit", "reject"]
    assert "50 000 000" in review_interrupt.value["draft"]

    result = compiled_graph.invoke(Command(resume={"decision": "approve"}), config)
    action_interrupt = _interrupts(result)[0]
    assert action_interrupt.value["type"] == "action_request"

    result = compiled_graph.invoke(Command(resume={"decisions": [{"type": "approve"}]}), config)
    assert not _interrupts(result)
    assert result["application_id"] == "APP-0001"
    assert len(graph_module.FAKE_CRM) == 1


def test_review_edit_replaces_offer_text(compiled_graph):
    config = _new_config()
    result = compiled_graph.invoke(
        {"customer_request": "Хочу автокредит на 50000000 сум на 36 месяцев", "review_round": 0},
        config,
    )
    result = _approve_all_docs(compiled_graph, config, result)

    custom_text = "Индивидуальное предложение для VIP-клиента."
    result = compiled_graph.invoke(
        Command(resume={"decision": "edit", "edited_text": custom_text}), config
    )
    action_interrupt = _interrupts(result)[0]
    assert action_interrupt.value["action_requests"][0]["args"]["offer_text"] == custom_text

    result = compiled_graph.invoke(Command(resume={"decisions": [{"type": "approve"}]}), config)
    assert result["offer_text"] == custom_text
    assert result["application_id"] == "APP-0001"


def test_review_reject_increments_round_and_loops_to_draft_offer(compiled_graph):
    config = _new_config()
    result = compiled_graph.invoke(
        {"customer_request": "Хочу автокредит на 50000000 сум на 36 месяцев", "review_round": 0},
        config,
    )
    result = _approve_all_docs(compiled_graph, config, result)
    assert result["review_round"] == 0 or "review_round" not in result

    result = compiled_graph.invoke(
        Command(resume={"decision": "reject", "feedback": "нужна другая ставка"}), config
    )
    # Rejected -> back to draft_offer -> new `review` interrupt, round incremented.
    assert result["review_round"] == 1
    assert result["review_feedback"] == "нужна другая ставка"
    review_interrupt = _interrupts(result)[0]
    assert review_interrupt.value["type"] == "review"
    assert "нужна другая ставка" in review_interrupt.value["draft"]


def test_review_rejected_three_times_escalates_to_operator(compiled_graph):
    config = _new_config()
    result = compiled_graph.invoke(
        {"customer_request": "Хочу автокредит на 50000000 сум на 36 месяцев", "review_round": 0},
        config,
    )
    result = _approve_all_docs(compiled_graph, config, result)

    for expected_round in (1, 2):
        result = compiled_graph.invoke(
            Command(resume={"decision": "reject", "feedback": f"замечание {expected_round}"}), config
        )
        assert result["review_round"] == expected_round
        assert _interrupts(result)[0].value["type"] == "review"

    # Third rejection hits MAX_REVIEW_ROUNDS -> escalates to operator_handoff
    # instead of looping back to draft_offer again.
    result = compiled_graph.invoke(
        Command(resume={"decision": "reject", "feedback": "замечание 3"}), config
    )
    assert result["review_round"] == 3
    interrupts = _interrupts(result)
    assert len(interrupts) == 1
    assert interrupts[0].value["type"] == "operator"

    result = compiled_graph.invoke(Command(resume="Оператор свяжется с клиентом лично."), config)
    assert not _interrupts(result)
    assert result.get("application_id") is None
    assert result["operator_reply"] == "Оператор свяжется с клиентом лично."


# --------------------------------------------------------------------------- #
# PATTERN 4 — tool-call approval gate
# --------------------------------------------------------------------------- #
def test_approval_gate_payload_mirrors_hitl_middleware_shape(compiled_graph):
    config = _new_config()
    result = compiled_graph.invoke(
        {"customer_request": "Хочу автокредит на 50000000 сум на 36 месяцев", "review_round": 0},
        config,
    )
    result = _approve_all_docs(compiled_graph, config, result)
    result = compiled_graph.invoke(Command(resume={"decision": "approve"}), config)

    payload = _interrupts(result)[0].value
    assert payload["type"] == "action_request"
    assert payload["action_requests"][0]["name"] == "submit_application"
    assert set(payload["action_requests"][0]["args"]) == {"amount", "term_months", "purpose", "offer_text"}
    assert payload["review_configs"] == [
        {"action_name": "submit_application", "allowed_decisions": ["approve", "edit", "reject"]}
    ]


def test_approval_gate_edit_overrides_only_given_args(compiled_graph):
    config = _new_config()
    result = compiled_graph.invoke(
        {"customer_request": "Хочу автокредит на 50000000 сум на 36 месяцев", "review_round": 0},
        config,
    )
    result = _approve_all_docs(compiled_graph, config, result)
    result = compiled_graph.invoke(Command(resume={"decision": "approve"}), config)

    result = compiled_graph.invoke(
        Command(resume={"type": "edit", "edited_action": {"args": {"amount": 45_000_000}}}), config
    )
    assert not _interrupts(result)
    assert result["application_id"] == "APP-0001"
    stored = graph_module.FAKE_CRM[-1]
    assert stored["action"]["args"]["amount"] == 45_000_000
    # term_months/purpose were not part of the edit -> must be preserved.
    assert stored["action"]["args"]["term_months"] == 36
    assert stored["action"]["args"]["purpose"] == "автокредит"
    # The CRM record and the graph state follow the HUMAN-edited figures,
    # not the pre-edit ones the graph computed on its own.
    assert stored["amount"] == 45_000_000
    assert result["amount"] == 45_000_000
    assert "45 000 000" in result["final_message"]
    assert "скорректированы" in result["offer_text"]


def test_approval_gate_reject_skips_submission(compiled_graph):
    config = _new_config()
    result = compiled_graph.invoke(
        {"customer_request": "Хочу автокредит на 50000000 сум на 36 месяцев", "review_round": 0},
        config,
    )
    result = _approve_all_docs(compiled_graph, config, result)
    result = compiled_graph.invoke(Command(resume={"decision": "approve"}), config)

    result = compiled_graph.invoke(
        Command(resume={"type": "reject", "message": "Заявка отклонена службой безопасности."}), config
    )
    assert not _interrupts(result)
    assert result.get("application_id") is None
    assert result["final_message"] == "Заявка отклонена службой безопасности."
    assert graph_module.FAKE_CRM == []


# --------------------------------------------------------------------------- #
# High-risk routing (deterministic, no interrupt of its own but drives PATTERN 5)
# --------------------------------------------------------------------------- #
def test_high_risk_skips_offer_pipeline_and_escalates(compiled_graph):
    config = _new_config()
    result = compiled_graph.invoke(
        {"customer_request": "Хочу ипотеку на 950000000 сум на 24 месяца", "review_round": 0},
        config,
    )
    result = _approve_all_docs(compiled_graph, config, result)

    assert result["risk_level"] == "high"
    assert result["dti"] > graph_module.DTI_HIGH
    interrupts = _interrupts(result)
    assert len(interrupts) == 1
    assert interrupts[0].value["type"] == "operator"
    assert "high" in interrupts[0].value["summary"]

    result = compiled_graph.invoke(Command(resume="Клиенту перезвонит риск-менеджер."), config)
    assert not _interrupts(result)
    assert result.get("application_id") is None
    assert result["operator_reply"] == "Клиенту перезвонит риск-менеджер."


# --------------------------------------------------------------------------- #
# End-to-end: full happy path with audit log + FAKE_CRM assertions
# --------------------------------------------------------------------------- #
def test_full_happy_path_end_to_end(compiled_graph):
    config = _new_config()
    result = compiled_graph.invoke(
        {"customer_request": "Хочу автокредит на 50000000 сум на 36 месяцев", "review_round": 0},
        config,
    )
    result = _approve_all_docs(compiled_graph, config, result)
    result = compiled_graph.invoke(Command(resume={"decision": "approve"}), config)
    result = compiled_graph.invoke(Command(resume={"decisions": [{"type": "approve"}]}), config)

    assert not _interrupts(result)
    assert result["application_id"] == "APP-0001"
    assert result["final_message"].startswith("Заявка APP-0001")
    assert len(graph_module.FAKE_CRM) == 1
    assert graph_module.FAKE_CRM[0]["amount"] == 50_000_000

    log = result["log"]
    # Every node that ran must have appended exactly one audit entry with its
    # own name-prefix, in the order the graph actually executed them.
    prefixes_in_order = [entry.split(":", 1)[0] for entry in log]
    assert prefixes_in_order == [
        "intake",
        "risk_check",
        "verify_document",
        "verify_document",
        "verify_document",
        "collect_verification",
        "draft_offer",
        "review_offer",
        "approval_gate",
        "submit_application",
        "notify",
    ]

    # Final LangChain message history carries the AIMessage from `notify`.
    assert result["messages"], "notify must append an AIMessage to `messages`"
    assert result["messages"][-1].content == result["final_message"]


def test_two_independent_threads_get_independent_application_ids(compiled_graph):
    """Sanity check that `thread_id` truly isolates checkpointed state, and
    that the FAKE_CRM/application_id counter is shared/global as documented
    (module-level, not per-thread) — two separate customers submitting in the
    same process get sequential ids."""
    for expected_id in ("APP-0001", "APP-0002"):
        config = _new_config()
        result = compiled_graph.invoke(
            {"customer_request": "Хочу автокредит на 50000000 сум на 36 месяцев", "review_round": 0},
            config,
        )
        result = _approve_all_docs(compiled_graph, config, result)
        result = compiled_graph.invoke(Command(resume={"decision": "approve"}), config)
        result = compiled_graph.invoke(Command(resume={"decisions": [{"type": "approve"}]}), config)
        assert result["application_id"] == expected_id
