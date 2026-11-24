"""Eval command — discover and run 10xGraph evaluations, always writing a JSON report."""

from __future__ import annotations

import asyncio
import sys
import webbrowser
from collections import defaultdict
from pathlib import Path
from typing import TYPE_CHECKING, Any

from tenxgraph.qa.evaluation.config.eval_config import ReporterConfig
from tenxgraph.qa.evaluation.eval_result import EvalCaseResult
from tenxgraph.qa.evaluation.eval_result import EvalReport as ER
from tenxgraph.qa.evaluation.reporters.manager import ReporterManager

from tenxgraph_api.cli.capabilities import OutputFormat
from tenxgraph_api.cli.commands import BaseCommand
from tenxgraph_api.cli.commands.eval.collect import _EvalCollector, _PendingCase, _PendingSimulation
from tenxgraph_api.cli.commands.eval.discovery import _EvalDiscovery
from tenxgraph_api.cli.commands.eval.display import _EvalDisplay
from tenxgraph_api.cli.commands.eval.run import _EvalRunner
from tenxgraph_api.cli.core.config import ConfigManager
from tenxgraph_api.import_path import is_import_path


if TYPE_CHECKING:
    from tenxgraph.qa.evaluation.eval_result import EvalReport


DEFAULT_CONCURRENCY = 4


class EvalCommand(_EvalDiscovery, _EvalCollector, _EvalDisplay, _EvalRunner, BaseCommand):
    """Discover and run agent evaluations; always write a JSON report."""

    # ------------------------------------------------------------------
    # Agent loading from 10xgraph.json
    # ------------------------------------------------------------------

    def _load_agent_from_config(self) -> Any:
        """Load the ``agent`` the way the server does: factories are called, the config folder is
        importable."""
        from tenxgraph_api.src.app.loader import load_graph

        config_manager = ConfigManager()
        discovered = config_manager.auto_discover_config()
        if not discovered:
            raise RuntimeError("No 10xgraph.json found — cannot auto-load agent.")
        config_manager.load_config(str(discovered))
        agent_spec: str = config_manager.get_config_value("agent", default="")
        if not is_import_path(agent_spec):
            raise RuntimeError(f"Invalid 'agent' field in 10xgraph.json: {agent_spec!r}")
        project_root = str(Path(discovered).resolve().parent)
        if project_root not in sys.path:
            sys.path.insert(0, project_root)
        return asyncio.run(load_graph(agent_spec))

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

    def execute(  # noqa: PLR0912, PLR0915
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

        # Record the pass-rate gate the exit code uses so the reports can state it.
        if isinstance(getattr(merged, "metadata", None), dict):
            merged.metadata["pass_rate_threshold"] = 1.0 if threshold is None else threshold
            merged.metadata["pass_rate_threshold_source"] = (
                "default" if threshold is None else "threshold"
            )

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


__all__ = [
    "DEFAULT_CONCURRENCY",
    "EvalCommand",
    "_PendingCase",
    "_PendingSimulation",
]
