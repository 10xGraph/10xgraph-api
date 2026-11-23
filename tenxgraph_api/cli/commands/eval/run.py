"""Eval command — discover and run 10xGraph evaluations, always writing a JSON report."""

from __future__ import annotations

import asyncio
import inspect
import time
from typing import TYPE_CHECKING, Any

from injectq.testing import override_dependency
from tenxgraph.qa.evaluation.collectors.trajectory_collector import (
    TrajectoryCollector,
    make_trajectory_callback,
)
from tenxgraph.qa.evaluation.eval_result import EvalCaseResult
from tenxgraph.utils.callbacks import CallbackManager

from tenxgraph_api.cli.commands.eval.collect import _PendingCase, _PendingSimulation
from tenxgraph_api.cli.core.steps import ProgressRun


if TYPE_CHECKING:
    from tenxgraph_api.cli.core.output import OutputFormatter


class _EvalRunner:
    """Progress printing and the flat pool run."""

    output: OutputFormatter

    # ------------------------------------------------------------------
    # Progress printing
    # ------------------------------------------------------------------

    @staticmethod
    def _case_status(result: EvalCaseResult) -> str:
        if result.passed:
            return "passed"
        return "error" if result.is_error else "failed"

    @staticmethod
    def _case_detail(result: EvalCaseResult) -> str:
        detail = f"{result.duration_seconds:.2f}s"
        tok = getattr(result, "token_usage", None)
        if tok and (tok.input_tokens or tok.output_tokens):
            detail += f"  in={tok.input_tokens} out={tok.output_tokens}"
        return detail

    @staticmethod
    def _accepts_on_event(run: Any) -> bool:
        try:
            params = inspect.signature(run).parameters
        except (TypeError, ValueError):
            return False
        return "on_event" in params or any(
            p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()
        )

    @staticmethod
    def _simulation_events(progress: ProgressRun, label: str) -> Any:
        """Build the ``on_event`` callback that turns simulator events into progress lines."""

        max_turns: list[int] = []

        def on_event(name: str, data: dict[str, Any]) -> None:
            if "max_turns" in data:
                max_turns[:] = [data["max_turns"]]
            turn = ""
            if "turn" in data:
                of = f"/{max_turns[0]}" if max_turns else ""
                turn = f"turn {data['turn']}{of}  "
            if name == "turn_started":
                progress.step(label, f"{turn}user: {data.get('user_message', '')}")
            elif name == "agent_replied":
                progress.step(label, f"{turn}agent: {data.get('response', '')}")
            elif name == "goals_checked":
                gained = data.get("newly_achieved") or []
                if gained:
                    progress.step(
                        label,
                        f"{turn}goals {data.get('achieved', 0)}/{data.get('total', 0)}"
                        f" (+ {', '.join(str(g) for g in gained)})",
                    )
            elif name == "evaluating":
                progress.step(label, f"evaluating criteria: {', '.join(data.get('criteria', []))}")
            elif name == "agent_error":
                progress.step(label, f"{turn}agent error: {data.get('error', '')}")

        return on_event

    # ------------------------------------------------------------------
    # Flat pool execution — single asyncio event loop for all cases
    # ------------------------------------------------------------------

    async def _run_flat_pool(  # noqa: PLR0915
        self,
        pending: list[_PendingCase | _PendingSimulation],
        max_concurrency: int,
        parallel: bool,
    ) -> list[tuple[str, str, str, EvalCaseResult]]:
        """Run all cases and simulations under a single event loop.

        Returns list of (file_name, eval_set_id, eval_set_name, EvalCaseResult).
        """
        total = len(pending)

        async def _run_case(
            pc: _PendingCase, progress: ProgressRun
        ) -> tuple[str, str, str, EvalCaseResult]:
            label = f"{pc.file_name}::{pc.case.name or pc.case.eval_id}"
            progress.step(label, "running")
            started = time.perf_counter()
            local_collector = TrajectoryCollector(
                capture_all_events=pc.evaluator.collector.capture_all_events,
            )
            _, local_mgr = make_trajectory_callback(local_collector)
            container = pc.evaluator.graph._state_graph._container
            with override_dependency(CallbackManager, local_mgr, container=container):
                try:
                    result = await pc.evaluator._evaluate_case(
                        pc.case, collector_override=local_collector
                    )
                except Exception as exc:
                    result = EvalCaseResult.failure(
                        eval_id=pc.case.eval_id,
                        error=str(exc),
                        name=pc.case.name,
                        duration_seconds=time.perf_counter() - started,
                        expected=pc.case.expected_summary(),
                    )
                if not result.duration_seconds:
                    result.duration_seconds = time.perf_counter() - started
            return (pc.file_name, pc.eval_set_id, pc.eval_set_name, result)

        async def _run_simulation(
            ps: _PendingSimulation, progress: ProgressRun
        ) -> tuple[str, str, str, EvalCaseResult]:
            from tenxgraph.qa.evaluation.eval_result import CriterionResult
            from tenxgraph.qa.evaluation.token_usage import TokenUsage

            sim_name = ps.scenario.description or ps.scenario.scenario_id
            label = f"{ps.file_name}::{sim_name}"
            run_kwargs: dict[str, Any] = {}
            # Older core releases have no on_event hook; run without step lines there.
            if self._accepts_on_event(ps.simulator.run):
                run_kwargs["on_event"] = self._simulation_events(progress, label)
            started = time.perf_counter()
            expected = {
                "input": getattr(ps.scenario, "starting_prompt", "") or None,
                "response": "; ".join(getattr(ps.scenario, "goals", None) or []) or None,
                "tool_calls": None,
                "node_order": None,
            }
            try:
                sim_result = await ps.simulator.run(ps.graph, ps.scenario, **run_kwargs)
                # Use the full CriterionResult objects (with per-criterion token_usage)
                # if available; fall back to rebuilding from scores dict.
                criterion_results = sim_result.criterion_results or [
                    CriterionResult.success(
                        criterion=name,
                        score=score,
                        threshold=ps.simulator.criteria[0].threshold
                        if ps.simulator.criteria
                        else 0.7,
                        details=sim_result.criterion_details.get(name, {}),
                    )
                    for name, score in sim_result.criterion_scores.items()
                ]
                # If no criterion ran (no goals defined), fall back to completion flag
                if not criterion_results:
                    score = 1.0 if sim_result.completed else 0.0
                    criterion_results = [
                        CriterionResult.success(
                            criterion="simulation_completed",
                            score=score,
                            threshold=0.5,
                        )
                    ]
                conversation_text = "\n".join(
                    f"{m['role'].upper()}: {m['content']}" for m in sim_result.conversation
                )
                criteria_token_usage = sum(
                    (cr.token_usage for cr in criterion_results), TokenUsage()
                )
                total_token_usage = sim_result.simulator_token_usage + criteria_token_usage
                result = EvalCaseResult.success(
                    eval_id=ps.scenario.scenario_id,
                    name=ps.scenario.description or ps.scenario.scenario_id,
                    criterion_results=criterion_results,
                    actual_response=conversation_text,
                    token_usage=total_token_usage,
                    agent_token_usage=sim_result.simulator_token_usage,
                    metadata={
                        "turns": sim_result.turns,
                        "goals_achieved": sim_result.goals_achieved,
                        "completed": sim_result.completed,
                    },
                    duration_seconds=time.perf_counter() - started,
                    expected=expected,
                )
            except Exception as exc:
                result = EvalCaseResult.failure(
                    eval_id=ps.scenario.scenario_id,
                    error=str(exc),
                    name=ps.scenario.description or ps.scenario.scenario_id,
                    duration_seconds=time.perf_counter() - started,
                    expected=expected,
                )
            return (ps.file_name, ps.eval_set_id, ps.eval_set_name, result)

        async def _dispatch(
            item: _PendingCase | _PendingSimulation, progress: ProgressRun
        ) -> tuple[str, str, str, EvalCaseResult]:
            if isinstance(item, _PendingSimulation):
                return await _run_simulation(item, progress)
            return await _run_case(item, progress)

        def _record(progress: Any, quad: tuple[str, str, str, EvalCaseResult]) -> None:
            file_name, _, _, result = quad
            progress.record(
                f"{file_name}::{result.name or result.eval_id}",
                status=self._case_status(result),
                detail=self._case_detail(result),
            )

        title = "Running evaluation cases" + (f" ({max_concurrency} at a time)" if parallel else "")

        if not parallel:
            results: list[tuple[str, str, str, EvalCaseResult]] = []
            with self.output.progress_run(title, total=total) as progress:
                for item in pending:
                    quad = await _dispatch(item, progress)
                    _record(progress, quad)
                    results.append(quad)
            return results

        semaphore = asyncio.Semaphore(max_concurrency)

        async def _run_one(
            item: _PendingCase | _PendingSimulation, progress: ProgressRun
        ) -> tuple[str, str, str, EvalCaseResult]:
            async with semaphore:
                return await _dispatch(item, progress)

        output_results: list[tuple[str, str, str, EvalCaseResult]] = []
        with self.output.progress_run(title, total=total) as progress:
            tasks = [asyncio.create_task(_run_one(item, progress)) for item in pending]
            for coro in asyncio.as_completed(tasks):
                quad = await coro
                _record(progress, quad)
                output_results.append(quad)

        return output_results
