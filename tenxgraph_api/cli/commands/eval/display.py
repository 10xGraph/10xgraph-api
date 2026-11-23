"""Eval command — discover and run 10xGraph evaluations, always writing a JSON report."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from tenxgraph.qa.evaluation import EvalConfig

from tenxgraph_api.cli.commands.eval.collect import _PendingCase, _PendingSimulation


if TYPE_CHECKING:
    from tenxgraph_api.cli.core.output import OutputFormatter


class _EvalDisplay:
    """Criteria display."""

    output: OutputFormatter

    # ------------------------------------------------------------------
    # Criteria display
    # ------------------------------------------------------------------

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
