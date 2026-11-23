"""Eval command — discover and run 10xGraph evaluations, always writing a JSON report."""

from __future__ import annotations

import importlib
import importlib.util
import inspect
import logging
import sys
import typing
from pathlib import Path
from typing import TYPE_CHECKING, Any

from tenxgraph.qa.evaluation import CriteriaConfig, CriterionConfig, EvalConfig


if TYPE_CHECKING:
    pass


class _EvalDiscovery:
    """Finding, loading and default config of eval files."""

    logger: logging.Logger

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
