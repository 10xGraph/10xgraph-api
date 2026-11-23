"""Eval command — discover and run 10xGraph evaluations, always writing a JSON report."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from tenxgraph.qa.evaluation import CriterionConfig, EvalConfig
from tenxgraph.qa.evaluation.collectors.trajectory_collector import (
    TrajectoryCollector,
)
from tenxgraph.qa.evaluation.evaluator import AgentEvaluator


if TYPE_CHECKING:
    import logging

    from tenxgraph_api.cli.core.output import OutputFormatter


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


class _EvalCollector:
    """Turn an eval file into pending cases and simulations."""

    logger: logging.Logger
    output: OutputFormatter

    if TYPE_CHECKING:

        def _load_module(self, path: Path) -> Any: ...

        def _default_config(self) -> EvalConfig: ...

        def _collect_eval_functions(self, mod: Any) -> tuple[list[tuple[str, Any]], Any]: ...

        def _load_agent_from_config(self) -> Any: ...

    # ------------------------------------------------------------------
    # Flat pool: collect all pending cases from a file
    # ------------------------------------------------------------------

    def _collect_from_file(
        self, path: Path, global_config: EvalConfig | None
    ) -> Sequence[_PendingCase | _PendingSimulation]:
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
