"""Safe interactive showcase for 10xGraph terminal motion."""

from __future__ import annotations

import time
from typing import Any

from tenxgraph_api.cli.commands import BaseCommand
from tenxgraph_api.cli.exceptions import ValidationError


class DemoCommand(BaseCommand):
    """Preview command-specific animations without external side effects."""

    _STYLES = ("typing", "network", "init", "build", "eval")

    _SUBTITLES = {
        "typing": "Full-screen 10xGraph identity and workspace transition",
        "network": "Live agent network and playground connection",
        "init": "Project scaffold assembly",
        "build": "Container delivery pipeline",
        "eval": "Evaluation scan and completion states",
    }
    _COMMAND_NAMES = {
        "typing": "typing",
        "network": "dev",
        "init": "init",
        "build": "build",
        "eval": "eval",
    }

    def execute(self, style: str = "all", **kwargs: Any) -> int:
        normalized = style.strip().lower()
        if normalized != "all" and normalized not in self._STYLES:
            return self.handle_error(
                ValidationError(
                    f"Unknown animation style '{style}'. "
                    f"Choose all, {', '.join(self._STYLES)}.",
                    field="style",
                )
            )

        selected = self._STYLES if normalized == "all" else (normalized,)
        for theme in selected:
            self.output.command_header(self._COMMAND_NAMES[theme], self._SUBTITLES[theme])

        self._preview_timeline()
        self._preview_progress()

        self.output.completion_screen(
            "Animation showcase",
            "All preview states rendered successfully",
            details={
                "Themes": ", ".join(selected),
                "Side effects": "none",
                "Fallback": "automatic for CI, pipes, and JSON",
            },
            next_steps=[
                "Run `10xgraph dev` to see the network theme in a real workflow.",
                "Use `10xgraph --no-animation COMMAND` for static accessible output.",
                "Use `10xgraph --fullscreen COMMAND` to hold a dedicated screen.",
            ],
        )
        return 0

    def _preview_timeline(self) -> None:
        stages = (
            ("topology", "Resolving graph topology", "3 nodes, 2 edges"),
            ("pipeline", "Assembling runtime pipeline", "checkpointer + store bound"),
            ("experience", "Connecting developer experience", "playground reachable"),
        )
        timeline = self.output.timeline(
            "Timeline preview",
            steps=tuple((key, title) for key, title, _ in stages),
        )
        with timeline:
            for key, _title, detail in stages:
                with timeline.step(key) as step:
                    time.sleep(0.35)
                    step.detail(detail)

    def _preview_progress(self) -> None:
        samples = (
            ("weather_eval::forecast_tokyo", "passed"),
            ("weather_eval::forecast_oslo", "passed"),
            ("weather_eval::unknown_city", "failed"),
            ("weather_eval::rate_limited", "passed"),
        )
        with self.output.progress_run("Progress preview", total=len(samples)) as progress:
            for label, status in samples:
                time.sleep(0.18)
                progress.record(label, status=status, detail="0.42s")
