"""Runs golden cases against the real agent graph (roadmap П.7б).

Two modes, one code path:

* **offline** (default) — `evals.stubs.offline_doubles()` replaces the LLM and
  every DB/HTTP loader. No `OPENAI_API_KEY`, no Postgres, no Weaviate; CI runs
  this on every PR so the harness cannot silently rot between paid runs.
* **live** (`--live`) — the real OpenAI client and the real database. This is
  the mode whose numbers mean something.

Everything else is identical: the same `Agent`, the same LangGraph graph, the
same nodes, tools and formatting. That is the whole point — an eval that runs
its own simplified copy of the routing logic measures the copy.

Two implementation choices worth knowing:

1. **Tool capture goes through LangChain callbacks**, not through a patched
   `ToolNode`. `app/agent/` is being refactored (`node_faq`'s hand-rolled loop
   is moving into a `create_agent` subgraph behind `CONSULTANT_AGENT_V2`), and
   a callback handler on the run config sees tool executions in either
   topology. The handler is injected by wrapping `build_run_config` — the one
   seam every agent invocation already goes through.
2. **Multi-step cases auto-advance** like `scripts/diag_agent.py` does: a
   product request lands in the qualification questionnaire first, so a
   single turn can never reach the product list. When a case is marked
   `must_reach_db` and the turn came back with buttons but no data, the runner
   presses the first button, up to `auto_advance` times.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable, Optional
from unittest.mock import patch

from langchain_core.callbacks import AsyncCallbackHandler

from evals.dataset import GOLDEN_CASES, iter_cases

# Auto-advance ceiling: the deepest qualification tree asks 3 questions before
# it can show products, so 3 button presses is what "reach the data" costs in
# the worst case. Higher would start measuring the questionnaire, not routing.
DEFAULT_AUTO_ADVANCE = 3

# A fixed user id keeps live runs out of real customers' profiles while still
# exercising the profile-load path.
EVAL_USER_ID = 999_001


@dataclass
class EvalResult:
    """One case's outcome — the input every evaluator scores."""

    case_id: str
    text: str
    lang: str
    tools: list[str] = field(default_factory=list)
    tool_args: list[dict] = field(default_factory=list)
    answer: str = ""
    keyboard_options: list[str] = field(default_factory=list)
    ui_blocks: list[dict] = field(default_factory=list)
    latency_ms: float = 0.0
    steps: int = 0
    token_usage: dict = field(default_factory=dict)
    cost: float = 0.0
    error: Optional[str] = None

    @property
    def tool_args_categories(self) -> list[str]:
        """Every `category` argument any tool received this turn — what
        `evaluators.category_match` scores."""
        return [
            str(args["category"]) for args in self.tool_args
            if isinstance(args, dict) and args.get("category")
        ]

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["tool_args_categories"] = self.tool_args_categories
        return data


class ToolCallRecorder(AsyncCallbackHandler):
    """Records every tool execution of the current case.

    Callback-based on purpose: it is the only capture point that survives both
    the current `node_faq` tool loop and the `create_agent` consultant subgraph
    that is replacing it, and it needs no changes inside `app/agent/`.
    """

    def __init__(self) -> None:
        self.names: list[str] = []
        self.args: list[dict] = []

    def reset(self) -> None:
        self.names.clear()
        self.args.clear()

    async def on_tool_start(
        self,
        serialized: dict,
        input_str: str,
        *,
        inputs: Optional[dict] = None,
        **kwargs: Any,
    ) -> None:
        name = (serialized or {}).get("name") or kwargs.get("name") or "<unknown>"
        self.names.append(str(name))
        args: dict = {}
        if isinstance(inputs, dict):
            args = dict(inputs)
        else:
            with contextlib.suppress(Exception):
                parsed = json.loads(input_str)
                if isinstance(parsed, dict):
                    args = parsed
        # `state` is LangGraph's InjectedState — noise in a report, and
        # potentially the whole conversation. Drop it.
        args.pop("state", None)
        self.args.append(args)


@dataclass
class Harness:
    """Everything a run needs: the agent, the tool recorder, and the reference
    data `evaluators.reached_db` compares answers against."""

    agent: Any
    recorder: ToolCallRecorder
    product_names: list[str]
    faq_texts: list[str]
    live: bool


async def load_reference_data() -> tuple[list[str], list[str]]:
    """Names of real catalogue entities (products AND offices) + FAQ answers.

    Offices are in the same list as products because `reached_db` asks one
    question — "did something that exists in our data reach the customer?" —
    and an office search answers it with a branch name exactly the way a
    product search answers it with a product name.

    Goes through `app.utils.data_loaders`, which means it returns the live DB
    contents in `--live` and the fixtures in offline mode without a branch:
    the offline doubles patch exactly those loaders. A failure degrades to
    empty lists — `reached_db` then falls back to structural evidence rather
    than failing every case.
    """
    from app.utils.data_loaders import (
        _load_card_product_offers,
        _load_credit_product_offers,
        _load_deposit_product_offers,
        _load_faq_items,
    )

    names: list[str] = []
    faq_texts: list[str] = []
    try:
        for loader in (_load_credit_product_offers, _load_deposit_product_offers,
                       _load_card_product_offers):
            for row in await loader():
                name = str(row.get("service_name") or "").strip()
                if name and name not in names:
                    names.append(name)
        # Every language: an answer only counts as "reached the DB" if the
        # exact stored text shows up, and a uz turn quotes the uz row.
        # NB the loader's keys are "q"/"a", not "question"/"answer".
        for language in ("ru", "en", "uz"):
            for item in await _load_faq_items(language):
                answer = str(item.get("a") or item.get("answer") or "").strip()
                if answer and answer not in faq_texts:
                    faq_texts.append(answer)
        from app.agent.branches import search_offices

        for office in await search_offices(query="", limit=10):
            for attr in ("name_ru", "name_uz"):
                name = str(getattr(office, attr, "") or "").strip()
                if name and name not in names:
                    names.append(name)
    except Exception:  # noqa: BLE001 — reference data is a nicety, never a blocker
        pass
    return names, faq_texts


@contextlib.asynccontextmanager
async def harness(*, live: bool = False):
    """Build (and tear down) everything a run needs.

    Env forced for BOTH modes:
    * `MEMORY_EXTRACT_ENABLED=false` — an eval must not write "facts" it just
      made up into a real customer profile.
    * `LANGGRAPH_CHECKPOINT_BACKEND=memory` — eval threads are throwaway and
      have no business in the production checkpoint table.
    """
    env = {
        "MEMORY_EXTRACT_ENABLED": "false",
        "LANGGRAPH_CHECKPOINT_BACKEND": "memory",
    }
    if not live:
        # Set before the agent is imported: config caches read these.
        env.setdefault("OPENAI_API_KEY", "offline-eval-stub-key")

    with patch.dict(os.environ, env):
        # Imported inside the context so the env above is in place first.
        from app.agent.agent import Agent

        recorder = ToolCallRecorder()

        with contextlib.ExitStack() as stack:
            stack.enter_context(_callback_injection(recorder))
            if not live:
                from evals.stubs import offline_doubles

                stack.enter_context(offline_doubles())

            agent = Agent()
            await agent.setup(backend="memory")
            product_names, faq_texts = await load_reference_data()
            try:
                yield Harness(
                    agent=agent,
                    recorder=recorder,
                    product_names=product_names,
                    faq_texts=faq_texts,
                    live=live,
                )
            finally:
                await agent.aclose()


@contextlib.contextmanager
def _callback_injection(recorder: ToolCallRecorder):
    """Attach *recorder* to every run config the agent builds.

    Wrapping `build_run_config` (rather than passing a config into
    `send_message`, which takes none) keeps `app/agent/` untouched and works
    for `_ainvoke`, `resume_human_mode` and any future call site alike.
    """
    from app.agent import agent as agent_module

    original = agent_module.build_run_config

    def _with_callbacks(*args: Any, **kwargs: Any) -> dict:
        config = original(*args, **kwargs)
        callbacks = list(config.get("callbacks") or [])
        callbacks.append(recorder)
        config["callbacks"] = callbacks
        return config

    with patch.object(agent_module, "build_run_config", _with_callbacks):
        yield


async def _seed_dialog(agent: Any, session_id: str, dialog: dict) -> None:
    """Write a starting dialog into the case's fresh checkpoint thread.

    A case like "the customer taps '1' after an office list" is only
    meaningful mid-conversation. Replaying the whole conversation to get
    there would measure the earlier turns instead, so the dialog is injected
    directly — the same thing a resumed production session hands the graph.
    """
    from app.agent.state import _default_dialog

    config = agent._build_config(session_id)
    merged = {**_default_dialog(), **dialog}
    await agent._graph.aupdate_state(config, {"dialog": merged})


def _has_db_evidence(result: EvalResult, product_names: Iterable[str],
                     faq_texts: Iterable[str]) -> bool:
    """Cheap re-use of the reached_db rule to decide whether to keep pressing
    buttons (see the auto-advance note in the module docstring)."""
    from evals.evaluators import reached_db

    verdict = reached_db(
        {"must_reach_db": True}, result.to_dict(),
        product_names=product_names, faq_texts=faq_texts,
    )
    return verdict["score"] >= 1.0


async def run_case(
    case: dict,
    *,
    harness_ctx: Harness,
    auto_advance: int = DEFAULT_AUTO_ADVANCE,
) -> EvalResult:
    """Run one case end to end and return its structured result."""
    result = EvalResult(case_id=case["id"], text=case["text"], lang=case["lang"])
    session_id = f"eval-{case['id']}-{uuid.uuid4().hex[:8]}"
    harness_ctx.recorder.reset()

    try:
        if case.get("dialog"):
            await _seed_dialog(harness_ctx.agent, session_id, case["dialog"])

        text = case["text"]
        max_steps = 1 + (auto_advance if case.get("must_reach_db") else 0)
        for _step in range(max_steps):
            started = time.perf_counter()
            turn = await harness_ctx.agent.send_message(
                session_id=session_id,
                user_id=EVAL_USER_ID,
                text=text,
                language=case["lang"],
                channel="eval",
            )
            result.latency_ms += (time.perf_counter() - started) * 1000.0
            result.steps += 1
            result.answer = str(turn.text or "")
            result.keyboard_options = list(turn.keyboard_options or [])
            result.ui_blocks = list(turn.ui_blocks or [])
            usage = turn.token_usage or {}
            for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
                if usage.get(key):
                    result.token_usage[key] = result.token_usage.get(key, 0) + int(usage[key])
            if usage.get("model"):
                result.token_usage["model"] = usage["model"]
            result.cost += float(usage.get("cost") or 0.0)

            if not case.get("must_reach_db"):
                break
            if _has_db_evidence(result, harness_ctx.product_names, harness_ctx.faq_texts):
                break
            if not result.keyboard_options:
                break
            # Press the first button — the questionnaire's own answer options.
            text = result.keyboard_options[0]

        result.tools = list(harness_ctx.recorder.names)
        result.tool_args = list(harness_ctx.recorder.args)
    except Exception as exc:  # noqa: BLE001 — one broken case must not end the run
        result.error = f"{type(exc).__name__}: {exc}"
        result.tools = list(harness_ctx.recorder.names)
        result.tool_args = list(harness_ctx.recorder.args)
    return result


async def run_dataset(
    cases: Optional[Iterable[dict]] = None,
    *,
    live: bool = False,
    auto_advance: int = DEFAULT_AUTO_ADVANCE,
    progress: Optional[Any] = None,
) -> tuple[list[EvalResult], Harness]:
    """Run every case sequentially; returns the results and the harness used.

    Sequential on purpose: the agent serializes turns per session anyway, the
    tool recorder is per-run state, and a live run hammering OpenAI in
    parallel would make the latency evaluator meaningless.
    """
    pool = list(GOLDEN_CASES if cases is None else cases)
    results: list[EvalResult] = []
    async with harness(live=live) as ctx:
        for index, case in enumerate(pool, start=1):
            result = await run_case(case, harness_ctx=ctx, auto_advance=auto_advance)
            results.append(result)
            if progress is not None:
                progress(index, len(pool), case, result)
        return results, ctx


async def _amain(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Run golden eval cases against the agent")
    parser.add_argument("--live", action="store_true", help="real OpenAI + DB (default: offline)")
    parser.add_argument("--offline", action="store_true", help="explicit offline mode (default)")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--filter", dest="name_filter", default=None)
    args = parser.parse_args(argv)

    cases = list(iter_cases(limit=args.limit, name_filter=args.name_filter))
    results, _ = await run_dataset(cases, live=args.live and not args.offline)
    for result in results:
        print(json.dumps(result.to_dict(), ensure_ascii=False))
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    return asyncio.run(_amain(argv))


if __name__ == "__main__":
    raise SystemExit(main())
