"""Eval progress steps, simulation duration, HTML report and browser opening."""

import asyncio
import dataclasses
import io
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from rich.console import Console
from tenxgraph.qa.evaluation import EvalConfig
from tenxgraph.qa.evaluation.eval_result import EvalCaseResult
from tenxgraph.qa.evaluation.token_usage import TokenUsage

from tenxgraph_api.cli.capabilities import OutputFormat
from tenxgraph_api.cli.commands.eval import EvalCommand, _PendingSimulation
from tenxgraph_api.cli.core.output import OutputFormatter
from tenxgraph_api.cli.core.steps import (
    LiveProgressRun,
    QuietProgressRun,
    StaticProgressRun,
    StructuredProgressRun,
    one_line,
)
from tenxgraph_api.cli.core.theme import UNICODE_GLYPHS


EvalCommand.__test__ = False


class _RecordingProgress:
    def __init__(self) -> None:
        self.steps: list[tuple[str, str]] = []
        self.records: list[tuple[str, str]] = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None

    def record(self, label, *, status, detail=""):
        self.records.append((label, status))

    def step(self, label, message):
        self.steps.append((label, message))


class _Out(OutputFormatter):
    def __init__(self, progress=None) -> None:
        super().__init__()
        self.progress = progress
        self.infos: list[str] = []
        self.warnings: list[str] = []

    def progress_run(self, title, *, total):
        return self.progress

    def info(self, message, emoji=True):
        self.infos.append(message)

    def warning(self, message, emoji=True):
        self.warnings.append(message)

    def error(self, message, emoji=True):
        pass

    def success(self, message, emoji=True):
        pass

    def print_banner(self, *args, **kwargs):
        pass


class _FakeSimulator:
    criteria: list = []

    async def run(self, graph, scenario, *, on_event=None):
        await asyncio.sleep(0.01)
        on_event("turn_started", {"turn": 1, "max_turns": 10, "user_message": "hi\nthere"})
        on_event("agent_replied", {"turn": 1, "response": "x" * 300})
        on_event(
            "goals_checked",
            {"turn": 1, "newly_achieved": ["greet"], "achieved": 1, "total": 2},
        )
        on_event("goals_checked", {"turn": 1, "newly_achieved": [], "achieved": 1, "total": 2})
        on_event("agent_error", {"turn": 2, "error": "boom"})
        on_event("evaluating", {"criteria": ["a", "b"]})
        return types.SimpleNamespace(
            criterion_results=[],
            criterion_scores={},
            criterion_details={},
            completed=True,
            conversation=[{"role": "user", "content": "hi"}],
            simulator_token_usage=TokenUsage(),
            turns=1,
            goals_achieved=["greet"],
        )


def _simulation() -> _PendingSimulation:
    scenario = types.SimpleNamespace(scenario_id="sim1", description="Greeting flow")
    return _PendingSimulation(
        scenario=scenario,
        graph=object(),
        simulator=_FakeSimulator(),
        file_name="f_eval.py",
        eval_set_id="s",
        eval_set_name="S",
    )


def test_simulation_steps_and_duration() -> None:
    progress = _RecordingProgress()
    cmd = EvalCommand(output=_Out(progress))
    quads = asyncio.run(cmd._run_flat_pool([_simulation()], 1, False))
    result = quads[0][3]
    assert result.duration_seconds > 0
    messages = [m for _, m in progress.steps]
    assert messages[0] == "turn 1/10  user: hi\nthere"  # raw; truncation is in the renderer
    assert any(m.startswith("turn 1/10  agent: ") for m in messages)
    assert "turn 1/10  goals 1/2 (+ greet)" in messages
    assert len([m for m in messages if "goals" in m]) == 1
    assert "turn 2/10  agent error: boom" in messages
    assert "evaluating criteria: a, b" in messages
    assert all(label == "f_eval.py::Greeting flow" for label, _ in progress.steps)


def test_case_gets_running_step_and_duration() -> None:
    progress = _RecordingProgress()
    cmd = EvalCommand(output=_Out(progress))
    case = types.SimpleNamespace(eval_id="c1", name="case one")

    async def _evaluate(case, collector_override=None):
        await asyncio.sleep(0.01)
        return EvalCaseResult.success(
            eval_id="c1", name="case one", criterion_results=[], actual_response=""
        )

    evaluator = MagicMock()
    evaluator._evaluate_case = _evaluate
    evaluator.collector.capture_all_events = False
    pc = types.SimpleNamespace(
        case=case, evaluator=evaluator, file_name="a_eval.py", eval_set_id="s", eval_set_name="S"
    )
    with (
        patch("tenxgraph_api.cli.commands.eval._PendingCase", types.SimpleNamespace),
        patch("tenxgraph_api.cli.commands.eval.override_dependency", MagicMock()),
        patch("tenxgraph_api.cli.commands.eval.make_trajectory_callback") as mk,
    ):
        mk.return_value = (None, None)
        quads = asyncio.run(cmd._run_flat_pool([pc], 1, False))
    assert progress.steps == [("a_eval.py::case one", "running")]
    assert quads[0][3].duration_seconds > 0


def test_one_line_collapses_and_truncates() -> None:
    assert one_line("a\n  b\tc") == "a b c"
    out = one_line("x" * 300)
    assert len(out) == 100 and out.endswith("...")


def test_static_step_line() -> None:
    emitted: list[str] = []
    run = StaticProgressRun(total=1, title="t", glyphs=UNICODE_GLYPHS, emit=emitted.append)
    with run:
        run.step("f::s", "turn 1/3  user: hello\nworld")
    assert emitted[-1].strip() == "f::s  turn 1/3  user: hello world"
    assert run.completed == 0


def test_structured_step_event() -> None:
    events: list[tuple[str, str, dict]] = []
    run = StructuredProgressRun(total=1, title="t", emit=lambda *a: events.append(a))
    with run:
        run.step("f::s", "running")
    assert ("eval_step", "f::s", {"message": "running"}) in events
    assert run.completed == 0


def test_live_step_line_does_not_advance() -> None:
    stream = io.StringIO()
    console = Console(file=stream, force_terminal=True, width=120, legacy_windows=False)
    with LiveProgressRun(console, total=2, title="t", glyphs=UNICODE_GLYPHS) as run:
        run.step("f::s", "turn 1/3  agent: hi")
    assert "f::s" in stream.getvalue() and "agent: hi" in stream.getvalue()
    assert run.completed == 0 and run.tally["passed"] == 0


def test_quiet_step_is_noop() -> None:
    run = QuietProgressRun(total=1, title="t")
    with run:
        run.step("a", "b")
    assert run.completed == 0


# ---------------------------------------------------------------------------
# Report + browser
# ---------------------------------------------------------------------------


def _execute(tmp_path: Path, *, caps: dict | None = None, env_ci: bool = False, **kwargs):
    out = _Out()
    out.capabilities = dataclasses.replace(
        out.capabilities, interactive=True, output_format=OutputFormat.HUMAN, is_ci=False
    )
    if caps:
        out.capabilities = dataclasses.replace(out.capabilities, **caps)
    cmd = EvalCommand(output=out)

    mock_cm = MagicMock()
    mock_cm.auto_discover_config.return_value = "10xgraph.json"
    mock_cm.get_evaluation_config.return_value = kwargs.pop("eval_cfg", {})
    report = MagicMock()
    report.metadata = {}
    report.summary.pass_rate = 1.0
    report.summary.passed_cases = 1
    report.summary.total_cases = 1
    report.summary.total_token_usage = TokenUsage()
    res = MagicMock()
    res.json_path = str(tmp_path / "r.json")
    res.html_path = str(tmp_path / "r.html")
    res.has_errors = False
    mgr = MagicMock()
    mgr.run_all.return_value = res
    case = EvalCaseResult.success(eval_id="c", name="c", criterion_results=[], actual_response="")
    fake = MagicMock()
    fake.config = EvalConfig()
    with (
        patch("tenxgraph_api.cli.commands.eval.ConfigManager", return_value=mock_cm),
        patch.object(cmd, "_discover", return_value=[Path("t_eval.py")]),
        patch.object(cmd, "_load_confeval", return_value=None),
        patch.object(cmd, "_collect_from_file", return_value=[fake]),
        patch.object(cmd, "_print_criteria_per_file"),
        patch.object(cmd, "_run_flat_pool", return_value=[("t", "id", "n", case)]),
        patch.object(cmd, "_merge_reports", return_value=report),
        patch("tenxgraph_api.cli.commands.eval.ReporterManager", return_value=mgr) as rm,
        patch("tenxgraph_api.cli.commands.eval.webbrowser.open", return_value=True) as wb,
    ):
        code = cmd.execute(target=str(tmp_path), **kwargs)
    return code, out, wb, rm


def test_html_and_json_requested_and_listed(tmp_path: Path) -> None:
    _, out, _, rm = _execute(tmp_path)
    cfg = rm.call_args.args[0]
    assert cfg.html is True and cfg.json_report is True and cfg.timestamp_files is True
    assert f"HTML report: {tmp_path / 'r.html'}" in out.infos
    assert f"JSON report: {tmp_path / 'r.json'}" in out.infos


def _recorded_metadata(rm) -> dict:
    return rm.return_value.run_all.call_args.args[0].metadata


def test_report_metadata_records_explicit_gate(tmp_path: Path) -> None:
    _, _, _, rm = _execute(tmp_path, threshold=0.62)
    meta = _recorded_metadata(rm)
    assert meta["pass_rate_threshold"] == 0.62
    assert meta["pass_rate_threshold_source"] == "threshold"


def test_report_metadata_records_default_gate(tmp_path: Path) -> None:
    _, _, _, rm = _execute(tmp_path)
    meta = _recorded_metadata(rm)
    assert meta["pass_rate_threshold"] == 1.0
    assert meta["pass_rate_threshold_source"] == "default"


def test_opens_html_once_by_default(tmp_path: Path) -> None:
    _, _, wb, _ = _execute(tmp_path)
    wb.assert_called_once_with((tmp_path / "r.html").resolve().as_uri())


@pytest.mark.parametrize(
    ("caps", "kwargs"),
    [
        (None, {"open_report": False}),
        (None, {"quiet": True}),
        (None, {"no_report": True}),
        (None, {"non_interactive": True}),
        ({"output_format": OutputFormat.JSON}, {}),
        ({"output_format": OutputFormat.JSONL}, {}),
        ({"interactive": False}, {}),
        ({"interactive": False, "is_ci": True}, {}),
        (None, {"eval_cfg": {"open_report": False}}),
    ],
)
def test_does_not_open(tmp_path: Path, caps, kwargs) -> None:
    _, _, wb, _ = _execute(tmp_path, caps=caps, **kwargs)
    wb.assert_not_called()


def test_cli_flag_beats_config(tmp_path: Path) -> None:
    _, _, wb, _ = _execute(tmp_path, open_report=True, eval_cfg={"open_report": False})
    wb.assert_called_once()


def test_open_failure_only_warns(tmp_path: Path) -> None:
    out = _Out()
    cmd = EvalCommand(output=out)
    with patch(
        "tenxgraph_api.cli.commands.eval.webbrowser.open", side_effect=RuntimeError("no display")
    ):
        cmd._open_report(str(tmp_path / "r.html"))
    assert out.warnings and "no display" in out.warnings[0]


def test_simulator_without_on_event_still_runs() -> None:
    class _Old(_FakeSimulator):
        async def run(self, graph, scenario):
            return await _FakeSimulator.run(self, graph, scenario, on_event=lambda *a: None)

    sim = _simulation()
    sim.simulator = _Old()
    progress = _RecordingProgress()
    quads = asyncio.run(EvalCommand(output=_Out(progress))._run_flat_pool([sim], 1, False))
    assert quads[0][3].error is None
    assert progress.steps == []


def test_simulation_result_carries_expected_start_and_goals() -> None:
    sim = _simulation()
    sim.scenario.starting_prompt = "Plan a trip"
    sim.scenario.goals = ["Get weather", "Get advice"]
    cmd = EvalCommand(output=_Out(_RecordingProgress()))
    result = asyncio.run(cmd._run_flat_pool([sim], 1, False))[0][3]
    assert result.expected == {
        "input": "Plan a trip",
        "response": "Get weather; Get advice",
        "tool_calls": None,
        "node_order": None,
    }


def test_failed_simulation_and_case_keep_expected_and_duration() -> None:
    class _Boom(_FakeSimulator):
        async def run(self, graph, scenario, *, on_event=None):
            await asyncio.sleep(0.01)
            raise RuntimeError("down")

    sim = _simulation()
    sim.simulator = _Boom()
    sim.scenario.starting_prompt = "Hi"
    sim.scenario.goals = ["g"]
    cmd = EvalCommand(output=_Out(_RecordingProgress()))
    failed = asyncio.run(cmd._run_flat_pool([sim], 1, False))[0][3]
    assert failed.error == "down"
    assert failed.expected["response"] == "g"
    assert failed.duration_seconds > 0
