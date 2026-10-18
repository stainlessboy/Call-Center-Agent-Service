"""CLI runner for the HITL demo graph (`examples/hitl_demo/graph.py`).

Usage (run from the repo root, with the project venv active):

    python -m examples.hitl_demo.run_demo                      # interactive
    python -m examples.hitl_demo.run_demo --auto                # scripted walkthrough
    python -m examples.hitl_demo.run_demo --auto --high-risk    # scripted, operator escalation
    python -m examples.hitl_demo.run_demo --time-travel         # auto run + time-travel demo
    python -m examples.hitl_demo.run_demo --mermaid             # print the graph diagram
    python -m examples.hitl_demo.run_demo --auto --llm          # let draft_offer call OpenAI

No `rich`/`colorama` dependency — plain `print()` with banner separators, so it
runs in any terminal with only the project's existing venv.
"""

from __future__ import annotations

import argparse
import uuid
from typing import Any

from langgraph.types import Command, Interrupt

from examples.hitl_demo.graph import FAKE_CRM, build_graph

BANNER = "=" * 78


def _section(title: str) -> None:
    print(f"\n{BANNER}\n{title}\n{BANNER}")


def _print_interrupt(interrupt: Interrupt) -> None:
    print(f"  [interrupt id={interrupt.id}]")
    for key, value in interrupt.value.items():
        print(f"    {key}: {value}")


def _new_config() -> dict[str, Any]:
    # Fixed thread_id per run — the checkpointer keys all state (including
    # pending interrupts) off this, so re-running with the SAME thread_id
    # would resume the *previous* run instead of starting fresh.
    return {"configurable": {"thread_id": str(uuid.uuid4())}}


def _pending_interrupts(result: dict[str, Any]) -> list[Interrupt]:
    """Interrupts are surfaced under the `"__interrupt__"` key of an
    `invoke()` result — a tuple/list of `Interrupt` objects, one per paused
    task. An equivalent, more introspectable way to check mid-session is
    `graph.get_state(config).interrupts` (or the older `.tasks[i].interrupts`)
    — see README.md "Как обнаружить незавершённый interrupt"."""
    return list(result.get("__interrupt__") or [])


# --------------------------------------------------------------------------- #
# Interactive mode
# --------------------------------------------------------------------------- #
def run_interactive(graph, use_llm: bool) -> None:
    _section("ИНТЕРАКТИВНЫЙ РЕЖИМ — Кредитная заявка с участием человека")
    config = _new_config()
    request = input("Опишите вашу заявку (сумма/срок/цель можно не указывать): ").strip()
    if not request:
        request = "Хочу кредит"

    result = graph.invoke({"customer_request": request, "review_round": 0}, config)

    while True:
        interrupts = _pending_interrupts(result)
        if not interrupts:
            break

        first_type = interrupts[0].value.get("type")

        if first_type == "ask_user":
            _section("PATTERN 1: запрос недостающих данных у клиента")
            _print_interrupt(interrupts[0])
            answer = input("Ваш ответ: ").strip()
            result = graph.invoke(Command(resume=answer), config)

        elif first_type == "verify_document":
            _section("PATTERN 2: параллельная проверка документов")
            resume_map: dict[str, Any] = {}
            for itr in interrupts:
                _print_interrupt(itr)
                doc = itr.value["document"]
                answer = input(f"  Документ «{doc}» подтверждён? (д/н): ").strip().lower()
                resume_map[itr.id] = answer in ("д", "да", "y", "yes")
            result = graph.invoke(Command(resume=resume_map), config)

        elif first_type == "review":
            _section("PATTERN 3: ревью предложения (approve/edit/reject)")
            _print_interrupt(interrupts[0])
            decision = input("Решение (approve/edit/reject): ").strip().lower()
            if decision == "edit":
                edited_text = input("Новый текст предложения: ")
                resume_value = {"decision": "edit", "edited_text": edited_text}
            elif decision == "reject":
                feedback = input("Обратная связь для доработки: ")
                resume_value = {"decision": "reject", "feedback": feedback}
            else:
                resume_value = {"decision": "approve"}
            result = graph.invoke(Command(resume=resume_value), config)

        elif first_type == "action_request":
            _section("PATTERN 4: подтверждение действия (tool-call approval gate)")
            _print_interrupt(interrupts[0])
            decision = input("Решение (approve/edit/reject): ").strip().lower()
            if decision == "edit":
                amount = input("Новая сумма (Enter — без изменений): ").strip()
                edited_action: dict[str, Any] = {"args": {}}
                if amount:
                    edited_action["args"]["amount"] = int(amount)
                resume_value = {"type": "edit", "edited_action": edited_action}
            elif decision == "reject":
                message = input("Сообщение клиенту об отказе: ")
                resume_value = {"type": "reject", "message": message}
            else:
                resume_value = {"decisions": [{"type": "approve"}]}
            result = graph.invoke(Command(resume=resume_value), config)

        elif first_type == "operator":
            _section("PATTERN 5: передача живому оператору")
            _print_interrupt(interrupts[0])
            reply = input("Ответ оператора клиенту: ").strip()
            result = graph.invoke(Command(resume=reply), config)

        else:  # pragma: no cover - defensive, all known types handled above
            print(f"Неизвестный тип interrupt: {first_type!r}, прерываю.")
            return

    _print_final(result)


# --------------------------------------------------------------------------- #
# Scripted ("--auto") mode
# --------------------------------------------------------------------------- #
def run_auto(graph, high_risk: bool) -> dict[str, Any]:
    """Fully scripted walkthrough exercising every pattern once:
    ask_user (missing term) -> parallel verify (all True) ->
    review reject-with-feedback once -> review edit -> approval edit -> submit.

    `--high-risk` swaps the customer request for a huge amount so the DTI
    check routes straight to `operator_handoff` after document verification,
    skipping the offer/review/approval nodes entirely.
    """
    _section("АВТОМАТИЧЕСКИЙ СЦЕНАРИЙ" + (" (высокий риск)" if high_risk else ""))
    config = _new_config()

    if high_risk:
        request = "Хочу ипотеку на 950000000 сум на 24 месяца"
    else:
        # Deliberately omit the term so PATTERN 1 (ask_user) fires once.
        request = "Хочу автокредит на 50000000 сум"

    print(f"Заявка клиента: {request!r}")
    result = graph.invoke({"customer_request": request, "review_round": 0}, config)

    # --- PATTERN 1: ask_user (only in the non-high-risk script) -------- #
    interrupts = _pending_interrupts(result)
    if interrupts and interrupts[0].value.get("type") == "ask_user":
        _section("PATTERN 1: ask_user")
        _print_interrupt(interrupts[0])
        answer = "36 месяцев"
        print(f"Резюме -> Command(resume={answer!r})")
        result = graph.invoke(Command(resume=answer), config)
        interrupts = _pending_interrupts(result)

    # --- PATTERN 2: parallel verify_document, all approved -------------- #
    _section("PATTERN 2: parallel verify_document")
    for itr in interrupts:
        _print_interrupt(itr)
    resume_map = {itr.id: True for itr in interrupts}
    print(f"Резюме -> Command(resume={ {k: v for k, v in resume_map.items()} !r})")
    result = graph.invoke(Command(resume=resume_map), config)
    interrupts = _pending_interrupts(result)

    if high_risk:
        # risk_level == "high" -> collect_verification routes straight to
        # operator_handoff, skipping draft_offer/review_offer/approval_gate.
        _section("PATTERN 5: operator_handoff (эскалация из-за высокого риска)")
        _print_interrupt(interrupts[0])
        reply = "Оператор: заявка передана в отдел рисков, ожидайте звонка."
        print(f"Резюме -> Command(resume={reply!r})")
        result = graph.invoke(Command(resume=reply), config)
        _print_final(result)
        return result

    # --- PATTERN 3a: review -> reject with feedback ---------------------- #
    _section("PATTERN 3: review_offer — reject с обратной связью")
    _print_interrupt(interrupts[0])
    resume_value: dict[str, Any] = {"decision": "reject", "feedback": "добавь информацию о первоначальном взносе"}
    print(f"Резюме -> Command(resume={resume_value!r})")
    result = graph.invoke(Command(resume=resume_value), config)
    interrupts = _pending_interrupts(result)

    # --- PATTERN 3b: review -> edit --------------------------------------- #
    _section("PATTERN 3: review_offer — edit")
    _print_interrupt(interrupts[0])
    edited_text = interrupts[0].value["draft"] + "\nПервоначальный взнос: 20%."
    resume_value = {"decision": "edit", "edited_text": edited_text}
    print("Резюме -> Command(resume={'decision': 'edit', 'edited_text': '<...с добавленным взносом...>'})")
    result = graph.invoke(Command(resume=resume_value), config)
    interrupts = _pending_interrupts(result)

    # --- PATTERN 4: approval_gate -> edit args ---------------------------- #
    _section("PATTERN 4: approval_gate — edit (изменение суммы)")
    _print_interrupt(interrupts[0])
    resume_value = {"type": "edit", "edited_action": {"args": {"amount": 45_000_000}}}
    print(f"Резюме -> Command(resume={resume_value!r})")
    result = graph.invoke(Command(resume=resume_value), config)

    _print_final(result)
    return result


def _print_final(result: dict[str, Any]) -> None:
    _section("ИТОГ")
    print(f"application_id: {result.get('application_id')}")
    print(f"final_message:\n{result.get('final_message')}")
    print("\nАудит (log):")
    for line in result.get("log", []):
        print(f"  - {line}")
    if FAKE_CRM:
        print(f"\nFAKE_CRM ({len(FAKE_CRM)} запись/и):")
        for entry in FAKE_CRM:
            print(f"  {entry}")


# --------------------------------------------------------------------------- #
# Time-travel demo
# --------------------------------------------------------------------------- #
def run_time_travel_demo(graph) -> None:
    """Demonstrates LangGraph's time-travel API:

        * `graph.get_state_history(config)` — every checkpoint of a thread,
          most recent first, each with `.config` (usable to resume/fork FROM
          that exact point) and `.next` (which node(s) would run next).
        * `graph.update_state(checkpoint_config, values)` — writes new
          channel values on top of a PAST checkpoint, creating a NEW
          checkpoint (a fork) that is a child of that past one, WITHOUT
          touching the original history at all.
        * `graph.invoke(None, forked_config)` — resumes execution from the
          forked checkpoint. Passing `None` (not `Command(resume=...)`)
          because that checkpoint is not sitting at a pending `interrupt()`.
    """
    _section("TIME TRAVEL — полный прогон + просмотр истории")
    config = _new_config()
    thread_id = config["configurable"]["thread_id"]
    print(f"thread_id: {thread_id}")

    # Full auto walkthrough on this exact thread_id so we get a rich history.
    request = "Хочу автокредит на 50000000 сум на 36 месяцев"
    print(f"Заявка клиента: {request!r}")
    result = graph.invoke({"customer_request": request, "review_round": 0}, config)
    interrupts = _pending_interrupts(result)
    result = graph.invoke(Command(resume={itr.id: True for itr in interrupts}), config)
    interrupts = _pending_interrupts(result)
    # approve immediately (no reject round, keep history compact for the demo)
    result = graph.invoke(Command(resume={"decision": "approve"}), config)
    interrupts = _pending_interrupts(result)
    result = graph.invoke(Command(resume={"decisions": [{"type": "approve"}]}), config)
    print(f"Первый прогон завершён: application_id={result.get('application_id')}")

    _section("История чекпоинтов (get_state_history)")
    history = list(graph.get_state_history(config))
    for snap in history:
        checkpoint_id = snap.config["configurable"]["checkpoint_id"]
        print(f"  checkpoint_id={checkpoint_id[:8]}...  next={snap.next}")

    # Find the checkpoint whose `.next` is exactly ("review_offer",) — i.e.
    # the state right BEFORE the review step ran.
    fork_point = None
    for snap in history:
        if snap.next == ("review_offer",):
            fork_point = snap
            break

    if fork_point is None:
        print("Не найден чекпоинт перед review_offer — пропускаю форк.")
        return

    _section("Форк истории (update_state на прошлом чекпоинте)")
    print(f"Форкаем от checkpoint_id={fork_point.config['configurable']['checkpoint_id'][:8]}...")
    forked_config = graph.update_state(
        fork_point.config,
        {"offer_text": fork_point.values["offer_text"] + "\n[ФОРК] Специальные условия для VIP-клиента."},
    )
    print(f"Новый checkpoint_id после форка: {forked_config['configurable']['checkpoint_id'][:8]}...")

    # Resume the fork — it's sitting right before review_offer, so it will
    # immediately hit a NEW `review` interrupt with the forked offer_text.
    forked_result = graph.invoke(None, forked_config)
    forked_interrupts = _pending_interrupts(forked_result)
    print("Новый interrupt на форке:")
    if forked_interrupts:
        _print_interrupt(forked_interrupts[0])
        print("draft содержит форкнутый текст:", "[ФОРК]" in forked_interrupts[0].value.get("draft", ""))

    _section("Исходная история осталась нетронутой")
    original_history_after = list(graph.get_state_history(config))
    print(f"Число чекпоинтов в исходном thread до форка: {len(history)}")
    print(f"Число чекпоинтов в том же thread после форка (форк добавил новые поверх): {len(original_history_after)}")
    print(
        "Чекпоинт, использованный для форка, всё ещё присутствует и не изменён — "
        "update_state создаёт НОВУЮ ветку, не переписывая прошлое."
    )


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def main() -> None:
    parser = argparse.ArgumentParser(
        description="Демо всех паттернов Human-in-the-Loop в LangGraph 1.x"
    )
    parser.add_argument("--auto", action="store_true", help="Полностью скриптованный прогон без stdin")
    parser.add_argument("--high-risk", action="store_true", help="Использовать сценарий с высоким риском (только с --auto)")
    parser.add_argument("--time-travel", action="store_true", help="Показать демо time-travel (форк истории)")
    parser.add_argument("--mermaid", action="store_true", help="Вывести Mermaid-диаграмму графа и выйти")
    parser.add_argument("--llm", action="store_true", help="Разрешить draft_offer использовать OpenAI при наличии OPENAI_API_KEY")
    args = parser.parse_args()

    graph = build_graph(use_llm=args.llm)

    if args.mermaid:
        _section("MERMAID ДИАГРАММА")
        print(graph.get_graph().draw_mermaid())
        return

    if args.time_travel:
        run_time_travel_demo(graph)
        return

    if args.auto:
        run_auto(graph, high_risk=args.high_risk)
        return

    run_interactive(graph, use_llm=args.llm)


if __name__ == "__main__":
    main()
