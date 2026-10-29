"""Eval command — discover and run 10xGraph evaluations, always writing a JSON report."""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import inspect
import sys
import time
import typing
import webbrowser
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from injectq.testing import override_dependency
from tenxgraph.qa.evaluation import CriteriaConfig, CriterionConfig, EvalConfig
from tenxgraph.qa.evaluation.collectors.trajectory_collector import (
    TrajectoryCollector,
    make_trajectory_callback,
)
from tenxgraph.qa.evaluation.config.eval_config import ReporterConfig
from tenxgraph.qa.evaluation.eval_result import EvalCaseResult
from tenxgraph.qa.evaluation.eval_result import EvalReport as ER
from tenxgraph.qa.evaluation.evaluator import AgentEvaluator
from tenxgraph.qa.evaluation.reporters.manager import ReporterManager
from tenxgraph.utils.callbacks import CallbackManager

from tenxgraph_api.cli.capabilities import OutputFormat
from tenxgraph_api.cli.commands import BaseCommand
from tenxgraph_api.cli.core.config import ConfigManager
from tenxgraph_api.cli.core.steps import ProgressRun


if TYPE_CHECKING:
    from tenxgraph.qa.evaluation.eval_result import EvalReport


@dataclass
class _PendingCase:
    case: Any  # EvalCase
    evaluator: AgentEvaluator
    config: Any  # EvalConfig — the resolved config for this case
    file_name: str
    eval_set_id: str
    eval_set_name: str
    config_source: str = ""  # where config came from: per-file / confeval.py / defaults


@dataclass
class _PendingSimulation:
    scenario: Any  # ConversationScenario
    graph: Any
    simulator: Any  # UserSimulator
    file_name: str
    eval_set_id: str
    eval_set_name: str


def _reset_inject_proxy(service_type: type) -> None:
    """Reset the lazy Inject[T] proxy cached on Node.execute's default parameters.

    Inject[T] proxies cache their resolved value in _injected_value on first use.
    container.override() clears the singleton scope, but the proxy's own cache is
    independent — it must be cleared so the proxy re-resolves from the container
    on next invocation instead of returning the stale value.
    """
    try:
        from tenxgraph.core.graph.node import Node as _GraphNode

        defaults = getattr(_GraphNode.execute, "__defaults__", None) or ()
        for d in defaults:
            if (
                hasattr(d, "_injected_value")
                and hasattr(d, "service_type")
                and d.service_type is service_type
            ):
                d._injected_value = None
    except Exception:  # nosec B110  # noqa: S110
        pass


DEFAULT_CONCURRENCY = 4


class EvalCommand(BaseCommand):
    """Discover and run agent evaluations; always write a JSON report."""

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    def _discover(self, target: Path) -> list[Path]:
        """Return eval files under target. If target is a file, return it directly."""
        if target.is_file():
            return [target]

        seen: dict[Path, None] = {}
        for pattern in ("*_eval.py", "eval_*.py"):
            for p in sorted(target.rglob(pattern)):
                seen[p] = None
        return list(seen)

    # ------------------------------------------------------------------
    # Module loading
    # ------------------------------------------------------------------

    def _load_module(self, path: Path) -> Any:
        project_root = str(Path.cwd())
        if project_root not in sys.path:
            sys.path.insert(0, project_root)

        spec = importlib.util.spec_from_file_location("_tenxgraph_eval", path)
        mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
        return mod

    # ------------------------------------------------------------------
    # Agent loading from 10xgraph.json
    # ------------------------------------------------------------------

    def _load_agent_from_config(self) -> Any:
        config_manager = ConfigManager()
        discovered = config_manager.auto_discover_config()
        if not discovered:
            raise RuntimeError("No 10xgraph.json found — cannot auto-load agent.")
        config_manager.load_config(str(discovered))
        agent_spec: str = config_manager.get_config_value("agent", default="")
        if not agent_spec or ":" not in agent_spec:
            raise RuntimeError(f"Invalid 'agent' field in 10xgraph.json: {agent_spec!r}")
        module_path, attr = agent_spec.rsplit(":", 1)
        mod = importlib.import_module(module_path)
        return getattr(mod, attr)

    # ------------------------------------------------------------------
    # Default config
    # ------------------------------------------------------------------

    def _default_config(self) -> EvalConfig:
        return EvalConfig(
            criteria=CriteriaConfig(
                tool_name_match=CriterionConfig.tool_name_match(threshold=1.0),
                # response_match=CriterionConfig.response_match(threshold=0.8),
                # hallucinations=CriterionConfig.hallucination(threshold=0.8),
                rouge_match=CriterionConfig.rouge_match(threshold=0.5),
                node_order=CriterionConfig.node_order(threshold=0.8),
            )
        )

    # ------------------------------------------------------------------
    # confeval.py loading
    # ------------------------------------------------------------------

    def _load_confeval(self, evals_dir: Path) -> EvalConfig | None:
        """Find and load confeval.py from evals_dir, returning its EvalConfig.

        Tries get_eval_config() first, then falls back to the EVAL_CONFIG
        constant.  Returns None if no confeval.py is found so callers can
        distinguish "no confeval" from "confeval with empty criteria".
        """
        confeval_path = evals_dir / "confeval.py"
        if not confeval_path.exists():
            return None

        try:
            mod = self._load_module(confeval_path)
        except Exception as exc:
            self.logger.warning("Failed to load confeval.py: %s", exc)
            return None

        if hasattr(mod, "get_eval_config"):
            try:
                return mod.get_eval_config()
            except Exception as exc:
                self.logger.warning("get_eval_config() in confeval.py failed: %s", exc)

        if hasattr(mod, "EVAL_CONFIG"):
            return mod.EVAL_CONFIG

        self.logger.warning(
            "confeval.py found but has no get_eval_config() or EVAL_CONFIG — ignoring"
        )
        return None

    def _confeval_search_dirs(self, target_path: Path, evals_dir: Path) -> list[Path]:
        """Directories to search for a global confeval.py, nearest-first.

        confeval.py is the GLOBAL criteria config, so its discovery must not
        depend on what the user targets. Walk up from the target (its parent
        when the target is a file) toward the current working directory, then
        append the configured evals directory. This way evals/confeval.py is
        found whether the user runs the whole evals/ dir, a subfolder, or a
        single file inside it.
        """
        start = target_path.parent if target_path.is_file() else target_path
        try:
            cur = start.resolve()
            cwd = Path.cwd().resolve()
        except OSError:
            return [evals_dir]

        dirs: list[Path] = []
        # Walk up only inside the project: a target outside the working directory must not
        # pick up a confeval.py (it is imported and run) from an unrelated parent folder.
        inside_project = cur == cwd or cwd in cur.parents
        while True:
            dirs.append(cur)
            if not inside_project or cur in (cwd, cur.parent):
                break
            cur = cur.parent

        ev = evals_dir.resolve()
        if ev not in dirs:
            dirs.append(ev)
        return dirs

    def _resolve_confeval(
        self, target_path: Path, evals_dir: Path
    ) -> tuple[EvalConfig | None, Path | None]:
        """Find and load the nearest global confeval.py.

        Returns (config, path_to_confeval) for the first directory that yields a
        usable confeval.py, or (None, None) when none is found so callers fall
        back to per-file config and then the built-in defaults.
        """
        for d in self._confeval_search_dirs(target_path, evals_dir):
            cfg = self._load_confeval(d)
            if cfg is not None:
                return cfg, d / "confeval.py"
        return None, None

    def _collect_eval_functions(self, mod: Any) -> tuple[list[tuple[str, Any]], Any]:
        """Pytest-style discovery: functions annotated -> EvalSet are evals, ->
        EvalConfig is config."""
        from tenxgraph.qa.evaluation import EvalConfig, EvalSet

        eval_pairs: list[tuple[str, Any]] = []
        config: Any = None

        for name, obj in inspect.getmembers(mod, inspect.isfunction):
            if name.startswith("_"):
                continue
            if getattr(obj, "__module__", None) != getattr(mod, "__name__", None):
                continue
            hints: dict[str, Any] = {}
            try:
                hints = typing.get_type_hints(obj)
            except Exception:
                hints = getattr(obj, "__annotations__", {})

            ret = hints.get("return")
            if ret is None:
                continue

            try:
                if inspect.isclass(ret) and issubclass(ret, EvalSet):
                    eval_pairs.append((name, obj()))
                    continue
                if inspect.isclass(ret) and issubclass(ret, EvalConfig) and config is None:
                    config = obj()
            except Exception as exc:
                self.logger.warning("Could not call %s(): %s", name, exc)

        return eval_pairs, config

    # ------------------------------------------------------------------
    # Flat pool: collect all pending cases from a file
    # ------------------------------------------------------------------

    def _collect_from_file(
        self, path: Path, global_config: EvalConfig | None
    ) -> list[_PendingCase | _PendingSimulation]:
        """Load a module and return pending work for every eval case or simulation scenario.

        Config priority chain (highest → lowest):
          1. Per-file      — EVAL_CONFIG / get_eval_config() inside the file
          2. confeval.py   — global_config (fallback when no per-file config)
          3. Built-in defaults — _default_config()

        Returns _PendingSimulation items when the file exposes get_scenarios() or SCENARIOS.
        Returns _PendingCase items for the standard get_eval_set() / pytest-style protocols.
        """
        mod = self._load_module(path)
        file_name = path.name

        # Simulator protocol — get_scenarios() or SCENARIOS constant
        scenarios = None
        if hasattr(mod, "get_scenarios"):
            try:
                scenarios = mod.get_scenarios()
            except Exception as exc:
                self.logger.warning("Could not call get_scenarios() in %s: %s", file_name, exc)
        elif hasattr(mod, "SCENARIOS"):
            scenarios = mod.SCENARIOS

        if scenarios is not None:
            return self._collect_simulations(mod, scenarios, file_name)

        # get_eval_set() protocol
        if hasattr(mod, "get_eval_set"):
            file_config: EvalConfig | None = None
            if hasattr(mod, "get_eval_config"):
                file_config = mod.get_eval_config()
            elif hasattr(mod, "EVAL_CONFIG"):
                file_config = mod.EVAL_CONFIG
            config, source = self._resolve_case_config(file_config, global_config)
            return self._make_pending(mod, mod.get_eval_set(), config, file_name, source)

        # pytest-style discovery
        eval_pairs, discovered_config = self._collect_eval_functions(mod)
        if eval_pairs:
            file_config = discovered_config or (
                mod.get_eval_config()
                if hasattr(mod, "get_eval_config")
                else mod.EVAL_CONFIG
                if hasattr(mod, "EVAL_CONFIG")
                else None
            )
            config, source = self._resolve_case_config(file_config, global_config)
            pending: list[_PendingCase] = []
            for _, es in eval_pairs:
                pending.extend(self._make_pending(mod, es, config, file_name, source))
            return pending

        self.output.warning(f"Skipping {file_name} — no eval entry point found.")
        return []

    def _resolve_case_config(
        self, file_config: EvalConfig | None, global_config: EvalConfig | None
    ) -> tuple[EvalConfig, str]:
        """Resolve the config for a file and label its source.

        Priority (highest first): per-file get_eval_config()/EVAL_CONFIG, then the
        global confeval.py, then the built-in defaults. The returned label is shown
        in the criteria block so users can see which level each file resolved to.
        """
        if file_config is not None:
            return file_config, "per-file"
        if global_config is not None:
            return global_config, "confeval.py"
        return self._default_config(), "built-in defaults"

    def _collect_simulations(
        self, mod: Any, scenarios: list[Any], file_name: str
    ) -> list[_PendingSimulation]:
        """Build _PendingSimulation items for each scenario in the file."""
        from tenxgraph.qa.evaluation import (
            CriterionConfig,
            SimulationGoalsCriterion,
            UserSimulator,
            UserSimulatorConfig,
        )

        graph = getattr(mod, "app", None) or self._load_agent_from_config()

        # Per-file simulator config via SIMULATOR_CONFIG constant or default
        sim_cfg: UserSimulatorConfig | None = getattr(mod, "SIMULATOR_CONFIG", None)
        goal_threshold: float = 0.7
        if sim_cfg is not None and hasattr(sim_cfg, "goal_threshold"):
            goal_threshold = sim_cfg.goal_threshold  # type: ignore[attr-defined]

        judge = SimulationGoalsCriterion(
            config=CriterionConfig(threshold=goal_threshold, num_samples=1)
        )
        simulator = UserSimulator(
            config=sim_cfg or UserSimulatorConfig(),
            criteria=[judge],
        )

        eval_set_id = f"{Path(file_name).stem}_simulations"
        eval_set_name = f"{Path(file_name).stem} (user simulator)"

        return [
            _PendingSimulation(
                scenario=sc,
                graph=graph,
                simulator=simulator,
                file_name=file_name,
                eval_set_id=eval_set_id,
                eval_set_name=eval_set_name,
            )
            for sc in scenarios
        ]

    def _make_pending(
        self,
        mod: Any,
        eval_set: Any,
        config: EvalConfig,
        file_name: str,
        config_source: str = "",
    ) -> list[_PendingCase]:
        graph = getattr(mod, "app", None) or self._load_agent_from_config()
        collector = TrajectoryCollector(capture_all_events=True)
        evaluator = AgentEvaluator(graph, collector, config=config)
        return [
            _PendingCase(
                case=c,
                evaluator=evaluator,
                config=config,
                file_name=file_name,
                eval_set_id=eval_set.eval_set_id,
                eval_set_name=eval_set.name,
                config_source=config_source,
            )
            for c in eval_set.eval_cases
        ]

    # ------------------------------------------------------------------
    # Criteria display
    # ------------------------------------------------------------------

    def _print_criteria_block(
        self,
        confeval_config: EvalConfig | None,
        confeval_path: Path | None,
    ) -> None:
        """Print the active global criteria and their source before any case runs.

        This is the global fallback config (confeval.py, else built-in defaults)
        applied to files without their own get_eval_config()/EVAL_CONFIG. Files
        that define a per-file config override this for their own cases.
        """
        if confeval_config is not None:
            source = str(confeval_path) if confeval_path else "confeval.py"
            criteria = confeval_config.criteria
        else:
            source = "built-in defaults"
            criteria = self._default_config().criteria

        lines = [f"Criteria  source: {source}"]
        for name in criteria.model_fields:
            cfg = getattr(criteria, name)
            if cfg is None:
                continue

            parts = [f"threshold={cfg.threshold}"]
            parts.append(cfg.match_type.value)
            if cfg.judge_model:
                parts.append(f"judge={cfg.judge_model}")
            if cfg.num_samples and cfg.num_samples != 1:
                parts.append(f"samples={cfg.num_samples}")
            lines.append(f"  {name:<40} {'  '.join(parts)}")
        self.output.info("\n".join(lines), emoji=False)

    def _criteria_rows(self, config: EvalConfig) -> list[tuple[str, str, dict[str, Any]]]:
        """Return (name, human_summary, machine_dict) for each active criterion."""
        criteria = config.criteria
        rows: list[tuple[str, str, dict[str, Any]]] = []
        for name in type(criteria).model_fields:
            cfg = getattr(criteria, name)
            if cfg is None:
                continue
            match_type = getattr(getattr(cfg, "match_type", None), "value", None)
            parts = [f"threshold={cfg.threshold}"]
            detail: dict[str, Any] = {"threshold": cfg.threshold}
            if match_type:
                parts.append(match_type)
                detail["match_type"] = match_type
            if cfg.judge_model:
                parts.append(f"judge={cfg.judge_model}")
                detail["judge_model"] = cfg.judge_model
            if cfg.num_samples and cfg.num_samples != 1:
                parts.append(f"samples={cfg.num_samples}")
                detail["num_samples"] = cfg.num_samples
            rows.append((name, "  ".join(parts), detail))
        return rows

    def _criteria_by_file(
        self, pending: list[_PendingCase | _PendingSimulation]
    ) -> dict[str, dict[str, Any]]:
        """Map each file to its resolved criteria source and active criteria.

        Used both for the per-file console block and the merged report metadata so
        a single combined run still records which criteria applied to which file.
        """
        out: dict[str, dict[str, Any]] = {}
        for pc in pending:
            if pc.file_name in out:
                continue
            if isinstance(pc, _PendingCase):
                rows = self._criteria_rows(pc.config)
                out[pc.file_name] = {
                    "source": pc.config_source,
                    "criteria": {name: detail for name, _, detail in rows},
                }
            elif isinstance(pc, _PendingSimulation):
                out[pc.file_name] = {"source": "user-simulator goals", "criteria": {}}
        return out

    def _print_criteria_per_file(
        self,
        pending: list[_PendingCase | _PendingSimulation],
        confeval_path: Path | None,
    ) -> None:
        """Print the criteria each file resolved to, before any case runs.

        Unlike the single global block, this shows per-file criteria so a combined
        run makes clear that each file is evaluated against its own config
        (per-file > confeval.py > built-in defaults).
        """
        seen: set[str] = set()
        blocks: list[str] = []
        for pc in pending:
            if pc.file_name in seen:
                continue
            seen.add(pc.file_name)

            if isinstance(pc, _PendingSimulation):
                blocks.append(f"Criteria  {pc.file_name}  (source: user-simulator goals)")
                continue

            source = pc.config_source
            if source == "confeval.py" and confeval_path:
                source = str(confeval_path)
            lines = [f"Criteria  {pc.file_name}  (source: {source})"]
            rows = self._criteria_rows(pc.config)
            if not rows:
                lines.append("  (no active criteria)")
            for name, summary, _ in rows:
                lines.append(f"  {name:<40} {summary}")
            blocks.append("\n".join(lines))
        if blocks:
            self.output.info("\n\n".join(blocks), emoji=False)

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
            # The Inject[CallbackManager] proxy on Node.execute caches its resolved
            # value independently of the container — reset it so it re-resolves
            # inside the override context instead of returning the stale instance.
            _reset_inject_proxy(CallbackManager)
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

    # ------------------------------------------------------------------
    # Report merging
    # ------------------------------------------------------------------

    def _merge_reports(self, reports: list[EvalReport], base_config: Any = None) -> EvalReport:
        if len(reports) == 1:
            return reports[0]

        all_results = []
        for r in reports:
            all_results.extend(r.results)
        return ER.create(
            eval_set_id="combined_eval",
            eval_set_name="Combined Evaluation",
            results=all_results,
            config_used=base_config.model_dump() if base_config else {},
        )

    # ------------------------------------------------------------------
    # Eval directory from 10xgraph.json
    # ------------------------------------------------------------------

    def _resolve_eval_dir(self) -> Path:
        config_manager = ConfigManager()
        discovered = config_manager.auto_discover_config()
        directory = "evals"
        if discovered:
            try:
                config_manager.load_config(str(discovered))
                eval_cfg = config_manager.get_evaluation_config()
                directory = eval_cfg.get("directory", "evals")
            except Exception:
                self.logger.warning(
                    "Failed to load eval directory from config; using default 'evals/'"
                )
        return Path.cwd() / directory

    def _should_open_report(
        self, open_report: bool | None, *, quiet: bool, non_interactive: bool
    ) -> bool:
        """Open the HTML report only for a person at an interactive terminal."""
        if open_report is False or quiet or non_interactive or self.output.quiet:
            return False
        # `interactive` is false when stdout is not a TTY or CI is set; structured
        # formats (json/jsonl) never run with an interactive human format.
        caps = self.output.capabilities
        return caps.interactive and caps.output_format == OutputFormat.HUMAN

    def _open_report(self, path: str) -> None:
        try:
            opened = webbrowser.open(Path(path).resolve().as_uri())
        except Exception as exc:
            self.output.warning(f"Could not open the HTML report: {exc}")
            return
        if not opened:
            self.output.warning("Could not open the HTML report in a browser; open the file above.")

    @staticmethod
    def _exit_code(pass_rate: float, threshold: float | None) -> int:
        """0 when the pass rate meets ``threshold``; without one, only a clean run passes."""
        required = 1.0 if threshold is None else threshold
        return 0 if pass_rate >= required else 1

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------

    def execute(  # noqa: PLR0912, PLR0913, PLR0915
        self,
        target: str | None = None,
        output_dir: str = "eval_reports",
        no_report: bool = False,
        threshold: float | None = None,
        parallel: bool = False,
        max_concurrency: int = 4,
        verbose: bool = False,
        quiet: bool = False,
        open_report: bool | None = None,
        non_interactive: bool = False,
        **kwargs: Any,
    ) -> int:
        # 1. Load infra settings from 10xgraph.json — no criteria here.
        # Criteria come exclusively from confeval.py (step 4) or per-file config.
        config_manager = ConfigManager()
        discovered = config_manager.auto_discover_config()
        json_directory = "evals"
        effective_parallel = parallel
        effective_concurrency = max_concurrency
        if discovered:
            try:
                config_manager.load_config(str(discovered))
                global_eval_cfg = config_manager.get_evaluation_config()
                if output_dir == "eval_reports":
                    output_dir = global_eval_cfg.get("output_dir", output_dir)
                if threshold is None:
                    threshold = global_eval_cfg.get("threshold")
                if not parallel:
                    effective_parallel = global_eval_cfg.get("parallel", False)
                if max_concurrency == DEFAULT_CONCURRENCY:
                    effective_concurrency = global_eval_cfg.get(
                        "max_concurrency", DEFAULT_CONCURRENCY
                    )
                json_directory = global_eval_cfg.get("directory", json_directory)
                if open_report is None and isinstance(global_eval_cfg.get("open_report"), bool):
                    open_report = global_eval_cfg["open_report"]
            except Exception:
                self.logger.warning("Failed to load eval config from 10xgraph.json; using defaults")

        # 2. CLI flags already captured as function parameters (highest priority).

        # 3. Resolve target path
        if target:
            target_path = Path(target)
            if not target_path.exists():
                self.output.error(f"Path not found: {target}")
                return 1
        else:
            target_path = Path.cwd() / json_directory
            if not target_path.exists():
                self.output.error(
                    f"Eval directory '{target_path}' not found. "
                    "Create an evals/ directory or pass a file/folder path."
                )
                return 1

        # 4. Load confeval.py → global criteria config (None if not found).
        #    confeval.py is the GLOBAL config: discover it independently of the
        #    target so it applies whether the user evaluates the whole evals/
        #    directory, a subfolder, or a single file inside it.
        evals_dir = Path.cwd() / json_directory
        confeval_config, confeval_path = self._resolve_confeval(target_path, evals_dir)

        # 5. Discover files
        files = self._discover(target_path)
        if not files:
            self.output.error(f"No eval files found in {target_path}")
            return 1

        # 6. Collect all pending cases across every file
        pending: list[_PendingCase | _PendingSimulation] = []
        for f in files:
            try:
                cases = self._collect_from_file(f, confeval_config)
                pending.extend(cases)
            except Exception as exc:
                self.output.error(f"Error loading {f.name}: {exc}")
                self.logger.exception("Failed to load eval file: %s", f)

        if not pending:
            self.output.error(
                "No eval cases found. Ensure eval files expose get_eval_set() "
                "or functions annotated with -> EvalSet."
            )
            return 1

        n_files = len({pc.file_name for pc in pending})
        n_sims = sum(1 for pc in pending if isinstance(pc, _PendingSimulation))
        n_cases = len(pending) - n_sims
        parts = []
        if n_cases:
            parts.append(f"{n_cases} eval case(s)")
        if n_sims:
            parts.append(f"{n_sims} simulation scenario(s)")
        self.output.command_header(
            "eval",
            f"Found {', '.join(parts)} across {n_files} file(s) in {target_path}",
        )

        # 7. Print per-file criteria so users see which config each file resolved to
        self._print_criteria_per_file(pending, confeval_path)

        # 8. Run all cases under a single asyncio event loop
        quads = asyncio.run(self._run_flat_pool(pending, effective_concurrency, effective_parallel))

        if not quads:
            self.output.error("No results produced.")
            return 1

        # Build per-eval-set config map from pending before results are consumed
        group_configs: dict[str, Any] = {
            pc.eval_set_id: pc.config for pc in pending if isinstance(pc, _PendingCase)
        }

        # 7. Group by eval_set_id → one EvalReport per set
        groups: dict[str, tuple[str, list[EvalCaseResult]]] = defaultdict(lambda: ("", []))
        for _, eval_set_id, eval_set_name, result in quads:
            name, results_list = groups[eval_set_id]
            groups[eval_set_id] = (eval_set_name or name, [*results_list, result])

        reports: list[EvalReport] = []
        for eval_set_id, (eval_set_name, results) in groups.items():
            group_cfg = group_configs.get(eval_set_id) or confeval_config or self._default_config()
            reports.append(
                ER.create(
                    eval_set_id=eval_set_id,
                    eval_set_name=eval_set_name,
                    results=results,
                    config_used=group_cfg.model_dump(),
                )
            )

        # 8. Merge into a single report
        merged = self._merge_reports(reports, base_config=confeval_config or self._default_config())

        # Record per-file criteria in the merged report so a single combined run still
        # captures which criteria applied to which file (the merged config_used alone
        # collapses to one config and loses this).
        if isinstance(getattr(merged, "metadata", None), dict):
            merged.metadata["criteria_by_file"] = self._criteria_by_file(pending)

        # 9. Determine exit code
        return_code = self._exit_code(merged.summary.pass_rate, threshold)
        if threshold is not None and return_code:
            self.output.error(
                f"Pass rate {merged.summary.pass_rate:.1%} is below threshold {threshold:.1%}"
            )

        # 10. Generate reports
        if not no_report:
            manager = ReporterManager(
                ReporterConfig(
                    output_dir=output_dir,
                    html=True,
                    json_report=True,
                    console=False,
                    timestamp_files=True,
                )
            )
            report_result = manager.run_all(merged)

            if report_result.html_path:
                self.output.info(f"HTML report: {report_result.html_path}", emoji=False)
            if report_result.json_path:
                self.output.info(f"JSON report: {report_result.json_path}", emoji=False)
                self.output.info(
                    "Also viewable in the playground: Evals > Open report file (pick the JSON).",
                    emoji=False,
                )
            if report_result.has_errors:
                for name, err in report_result.errors:
                    self.output.warning(f"Reporter error [{name}]: {err}")
            if report_result.html_path and self._should_open_report(
                open_report, quiet=quiet, non_interactive=non_interactive
            ):
                self._open_report(report_result.html_path)

        summary = merged.summary
        self.output.info(
            f"Results: {summary.passed_cases}/{summary.total_cases} passed "
            f"({summary.pass_rate:.1%})",
            emoji=False,
        )
        tok = summary.total_token_usage
        if tok.total_tokens:
            cache_part = f"  cache_read: {tok.cache_read_tokens:,}" if tok.cache_read_tokens else ""
            self.output.info(
                f"Tokens  — input: {tok.input_tokens:,}  output: {tok.output_tokens:,}"
                f"{cache_part}  total: {tok.total_tokens:,}",
                emoji=False,
            )

        return return_code
