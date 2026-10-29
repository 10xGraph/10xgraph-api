import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from tenxgraph.qa.evaluation import CriteriaConfig, CriterionConfig, EvalConfig
from tenxgraph.qa.evaluation.eval_result import EvalCaseResult

from tenxgraph_api.cli.commands.eval import EvalCommand, _PendingCase, _PendingSimulation
from tenxgraph_api.cli.core.output import OutputFormatter


# Disable pytest collection for the imported EvalCommand class
EvalCommand.__test__ = False


class _SilentOutput(OutputFormatter):
    def __init__(self) -> None:
        super().__init__()
        self.successes = []
        self.errors = []
        self.infos = []
        self.warnings = []

    def success(self, message: str, emoji: bool = True) -> None:
        self.successes.append(message)

    def error(self, message: str, emoji: bool = True) -> None:
        self.errors.append(message)

    def info(self, message: str, emoji: bool = True) -> None:
        self.infos.append(message)

    def warning(self, message: str, emoji: bool = True) -> None:
        self.warnings.append(message)

    def print_banner(self, *args, **kwargs) -> None:
        pass


@pytest.fixture
def cmd() -> EvalCommand:
    return EvalCommand(output=_SilentOutput())


def test_load_agent_from_config_success(cmd):
    mock_cm = MagicMock()
    mock_cm.auto_discover_config.return_value = "10xgraph.json"
    mock_cm.get_config_value.return_value = "my_module:my_agent"

    mock_agent = MagicMock()
    mock_module = types.SimpleNamespace(my_agent=mock_agent)

    with (
        patch("tenxgraph_api.cli.commands.eval.ConfigManager", return_value=mock_cm),
        patch("importlib.import_module", return_value=mock_module) as mock_import,
    ):
        agent = cmd._load_agent_from_config()
        assert agent is mock_agent
        mock_import.assert_called_once_with("my_module")


def test_load_agent_from_config_no_json(cmd):
    mock_cm = MagicMock()
    mock_cm.auto_discover_config.return_value = None

    with patch("tenxgraph_api.cli.commands.eval.ConfigManager", return_value=mock_cm):
        with pytest.raises(RuntimeError, match="No 10xgraph.json found"):
            cmd._load_agent_from_config()


def test_load_agent_from_config_invalid_spec(cmd):
    mock_cm = MagicMock()
    mock_cm.auto_discover_config.return_value = "10xgraph.json"
    mock_cm.get_config_value.return_value = "invalid_spec"

    with patch("tenxgraph_api.cli.commands.eval.ConfigManager", return_value=mock_cm):
        with pytest.raises(RuntimeError, match="Invalid 'agent' field"):
            cmd._load_agent_from_config()


def test_print_criteria_block(cmd):
    cfg = EvalConfig(
        criteria=CriteriaConfig(
            tool_name_match=CriterionConfig.tool_name_match(threshold=1.0),
        )
    )
    cmd._print_criteria_block(cfg, Path("some_dir"))
    rendered = "\n".join(cmd.output.infos)
    assert "Criteria  source:" in rendered
    assert "tool_name_match" in rendered


def test_case_progress_fields_for_a_passing_case(cmd):
    res = EvalCaseResult.success(
        eval_id="c1",
        name="case1",
        criterion_results=[],
        actual_response="",
    )
    res.duration_seconds = 1.23
    assert cmd._case_status(res) == "passed"
    assert "1.23s" in cmd._case_detail(res)


def test_resolve_eval_dir(cmd):
    mock_cm = MagicMock()
    mock_cm.auto_discover_config.return_value = "10xgraph.json"
    mock_cm.get_evaluation_config.return_value = {"directory": "custom_evals"}

    with patch("tenxgraph_api.cli.commands.eval.ConfigManager", return_value=mock_cm):
        eval_dir = cmd._resolve_eval_dir()
        assert eval_dir == Path.cwd() / "custom_evals"


def test_collect_simulations(cmd):
    mod = types.SimpleNamespace(app=MagicMock(), SIMULATOR_CONFIG=MagicMock())
    scenarios = [MagicMock(scenario_id="s1", description="desc1")]

    with patch("tenxgraph_api.cli.commands.eval.ConfigManager"):
        res = cmd._collect_simulations(mod, scenarios, "file_eval.py")
        assert len(res) == 1
        assert isinstance(res[0], _PendingSimulation)
        assert res[0].eval_set_id == "file_eval_simulations"


@pytest.mark.asyncio
async def test_run_flat_pool_cases(cmd):
    case1 = MagicMock()
    case1.eval_id = "c1"
    case1.name = "case_name"
    evaluator = MagicMock()

    async def mock_evaluate_case(case, collector_override):
        return EvalCaseResult.success(
            eval_id="c1",
            name="case_name",
            criterion_results=[],
            actual_response="resp",
        )

    evaluator._evaluate_case = mock_evaluate_case
    evaluator.collector.capture_all_events = True

    pc = _PendingCase(
        case=case1,
        evaluator=evaluator,
        config=EvalConfig(),
        file_name="test_file.py",
        eval_set_id="es1",
        eval_set_name="Eval Set 1",
    )

    with (
        patch("tenxgraph_api.cli.commands.eval._reset_inject_proxy"),
        patch("tenxgraph_api.cli.commands.eval.override_dependency"),
    ):
        results = await cmd._run_flat_pool([pc], max_concurrency=4, parallel=False)
        assert len(results) == 1
        assert results[0][0] == "test_file.py"
        assert results[0][1] == "es1"
        assert results[0][3].passed is True


@pytest.mark.asyncio
async def test_run_flat_pool_case_error(cmd):
    case1 = MagicMock()
    case1.eval_id = "c1"
    case1.name = "case_name"
    evaluator = MagicMock()

    async def mock_evaluate_case(case, collector_override):
        raise ValueError("evaluation failed")

    evaluator._evaluate_case = mock_evaluate_case
    evaluator.collector.capture_all_events = True

    pc = _PendingCase(
        case=case1,
        evaluator=evaluator,
        config=EvalConfig(),
        file_name="test_file.py",
        eval_set_id="es1",
        eval_set_name="Eval Set 1",
    )

    with (
        patch("tenxgraph_api.cli.commands.eval._reset_inject_proxy"),
        patch("tenxgraph_api.cli.commands.eval.override_dependency"),
    ):
        results = await cmd._run_flat_pool([pc], max_concurrency=4, parallel=False)
        assert len(results) == 1
        assert results[0][3].passed is False
        assert "evaluation failed" in results[0][3].error


@pytest.mark.asyncio
async def test_run_flat_pool_simulation(cmd):
    from tenxgraph.qa.evaluation.token_usage import TokenUsage

    # Mock simulator
    simulator = MagicMock()
    mock_criterion = MagicMock()
    mock_criterion.threshold = 0.7
    simulator.criteria = [mock_criterion]

    sim_result = MagicMock()
    sim_result.completed = True
    sim_result.criterion_results = []
    sim_result.criterion_scores = {"g1": 1.0}
    sim_result.criterion_details = {}
    sim_result.conversation = [{"role": "user", "content": "hello"}]
    sim_result.simulator_token_usage = TokenUsage(input_tokens=10, output_tokens=5)
    sim_result.turns = 2
    sim_result.goals_achieved = 1

    async def mock_simulator_run(graph, scenario):
        return sim_result

    simulator.run = mock_simulator_run

    ps = _PendingSimulation(
        scenario=MagicMock(scenario_id="sc1", description="sc_desc"),
        graph=MagicMock(),
        simulator=simulator,
        file_name="test_sim.py",
        eval_set_id="es_sim",
        eval_set_name="Sim Set",
    )

    results = await cmd._run_flat_pool([ps], max_concurrency=4, parallel=False)
    assert len(results) == 1
    assert results[0][3].passed is True
    assert "USER: hello" in results[0][3].actual_response


@pytest.mark.asyncio
async def test_run_flat_pool_simulation_error(cmd):
    simulator = MagicMock()
    mock_criterion = MagicMock()
    mock_criterion.threshold = 0.7
    simulator.criteria = [mock_criterion]

    async def mock_simulator_run(graph, scenario):
        raise ValueError("simulation failed")

    simulator.run = mock_simulator_run

    ps = _PendingSimulation(
        scenario=MagicMock(scenario_id="sc1", description="sc_desc"),
        graph=MagicMock(),
        simulator=simulator,
        file_name="test_sim.py",
        eval_set_id="es_sim",
        eval_set_name="Sim Set",
    )

    results = await cmd._run_flat_pool([ps], max_concurrency=4, parallel=False)
    assert len(results) == 1
    assert results[0][3].passed is False
    assert "simulation failed" in results[0][3].error


def test_execute_target_not_found(cmd):
    with patch("tenxgraph_api.cli.commands.eval.ConfigManager"):
        code = cmd.execute(target="non_existent_path")
        assert code == 1
        assert len(cmd.output.errors) > 0


def test_execute_no_files(cmd, tmp_path):
    with (
        patch("tenxgraph_api.cli.commands.eval.ConfigManager"),
        patch.object(cmd, "_discover", return_value=[]),
    ):
        code = cmd.execute(target=str(tmp_path))
        assert code == 1
        assert len(cmd.output.errors) > 0


def test_execute_success(cmd, tmp_path):
    from tenxgraph.qa.evaluation.token_usage import TokenUsage

    # Mock ConfigManager and its returns
    mock_cm = MagicMock()
    mock_cm.auto_discover_config.return_value = "10xgraph.json"
    mock_cm.get_evaluation_config.return_value = {}

    # Mock discovery and collection
    fake_case = MagicMock()
    fake_case.config = EvalConfig()

    mock_report = MagicMock()
    mock_report.summary.pass_rate = 1.0
    mock_report.summary.passed_cases = 1
    mock_report.summary.total_cases = 1
    mock_report.summary.total_token_usage = TokenUsage(input_tokens=100, output_tokens=50)

    mock_rep_mgr_res = MagicMock()
    mock_rep_mgr_res.json_path = "path/to/report.json"
    mock_rep_mgr_res.has_errors = False

    mock_rep_mgr = MagicMock()
    mock_rep_mgr.run_all.return_value = mock_rep_mgr_res

    # We run flat pool which returns quads
    mock_case_result = EvalCaseResult.success(
        eval_id="case_id",
        name="case_name",
        criterion_results=[],
        actual_response="resp",
        token_usage=TokenUsage(input_tokens=10, output_tokens=5),
    )
    quads = [("test_eval.py", "eval_set_id", "eval_set_name", mock_case_result)]

    with (
        patch("tenxgraph_api.cli.commands.eval.ConfigManager", return_value=mock_cm),
        patch.object(cmd, "_discover", return_value=[Path("test_eval.py")]),
        patch.object(cmd, "_load_confeval", return_value=None),
        patch.object(cmd, "_collect_from_file", return_value=[fake_case]),
        patch.object(cmd, "_print_criteria_block"),
        patch.object(cmd, "_run_flat_pool", return_value=quads),
        patch.object(cmd, "_merge_reports", return_value=mock_report),
        patch("tenxgraph_api.cli.commands.eval.ReporterManager", return_value=mock_rep_mgr),
    ):
        code = cmd.execute(target=str(tmp_path))
        assert code == 0
        assert any("Open it in the playground" in m for m in cmd.output.infos)


def test_execute_below_threshold(cmd, tmp_path):
    from tenxgraph.qa.evaluation.token_usage import TokenUsage

    # Mock ConfigManager and its returns
    mock_cm = MagicMock()
    mock_cm.auto_discover_config.return_value = "10xgraph.json"
    mock_cm.get_evaluation_config.return_value = {}

    fake_case = MagicMock()
    fake_case.config = EvalConfig()

    mock_report = MagicMock()
    mock_report.summary.pass_rate = 0.5
    mock_report.summary.passed_cases = 1
    mock_report.summary.total_cases = 2
    mock_report.summary.total_token_usage = TokenUsage(input_tokens=100, output_tokens=50)

    mock_rep_mgr = MagicMock()
    mock_rep_mgr.run_all.return_value = MagicMock(html_path=None, json_path=None, has_errors=False)

    mock_case_result = EvalCaseResult.failure(
        eval_id="case_id",
        name="case_name",
        error="failed",
    )
    quads = [("test_eval.py", "eval_set_id", "eval_set_name", mock_case_result)]

    with (
        patch("tenxgraph_api.cli.commands.eval.ConfigManager", return_value=mock_cm),
        patch.object(cmd, "_discover", return_value=[Path("test_eval.py")]),
        patch.object(cmd, "_load_confeval", return_value=None),
        patch.object(cmd, "_collect_from_file", return_value=[fake_case]),
        patch.object(cmd, "_print_criteria_block"),
        patch.object(cmd, "_run_flat_pool", return_value=quads),
        patch.object(cmd, "_merge_reports", return_value=mock_report),
        patch("tenxgraph_api.cli.commands.eval.ReporterManager", return_value=mock_rep_mgr),
    ):
        code = cmd.execute(target=str(tmp_path), threshold=0.8)
        assert code == 1
        assert "below threshold" in cmd.output.errors[0]


def test_execute_collect_error(cmd, tmp_path):
    with (
        patch("tenxgraph_api.cli.commands.eval.ConfigManager"),
        patch.object(cmd, "_discover", return_value=[Path("test_eval.py")]),
        patch.object(cmd, "_collect_from_file", side_effect=ValueError("load error")),
    ):
        code = cmd.execute(target=str(tmp_path))
        assert code == 1
        assert "Error loading test_eval.py: load error" in cmd.output.errors[0]


def test_reset_inject_proxy():
    from tenxgraph_api.cli.commands.eval import _reset_inject_proxy

    _reset_inject_proxy(None)


def test_load_module(cmd, tmp_path):
    f = tmp_path / "dummy_mod.py"
    f.write_text("x = 42")
    mod = cmd._load_module(f)
    assert mod.x == 42


def test_load_confeval_func_error(cmd, tmp_path):
    confeval = tmp_path / "confeval.py"
    confeval.write_text("")

    def raise_err():
        raise ValueError("config error")

    fake_mod = types.SimpleNamespace(get_eval_config=raise_err, EVAL_CONFIG="some_config")

    with patch.object(cmd, "_load_module", return_value=fake_mod):
        result = cmd._load_confeval(tmp_path)
        assert result == "some_config"


def test_collect_eval_functions(cmd):
    from tenxgraph.qa.evaluation import EvalConfig, EvalSet

    class FakeEvalSet(EvalSet):
        def __init__(self):
            super().__init__(eval_set_id="es1", name="Set 1", eval_cases=[])

    class FakeEvalConfig(EvalConfig):
        pass

    def func_eval() -> FakeEvalSet:
        return FakeEvalSet()

    def func_config() -> FakeEvalConfig:
        return FakeEvalConfig()

    fake_mod = types.SimpleNamespace(__name__="fake_mod")
    func_eval.__module__ = "fake_mod"
    func_config.__module__ = "fake_mod"
    fake_mod.func_eval = func_eval
    fake_mod.func_config = func_config

    eval_pairs, config = cmd._collect_eval_functions(fake_mod)
    assert len(eval_pairs) == 1
    assert eval_pairs[0][0] == "func_eval"
    assert isinstance(config, FakeEvalConfig)


def test_collect_from_file_scenarios_error(cmd, tmp_path):
    p = tmp_path / "x_eval.py"
    p.write_text("")

    def raise_err():
        raise ValueError("scenarios error")

    fake_mod = types.SimpleNamespace(get_scenarios=raise_err)
    with patch.object(cmd, "_load_module", return_value=fake_mod):
        res = cmd._collect_from_file(p, None)
        assert res == []


def test_make_pending_loads_agent_from_config(cmd):
    fake_mod = types.SimpleNamespace()
    eval_set = MagicMock()
    eval_set.eval_cases = [MagicMock()]
    config = EvalConfig()

    mock_agent = MagicMock()
    with patch.object(cmd, "_load_agent_from_config", return_value=mock_agent):
        res = cmd._make_pending(fake_mod, eval_set, config, "file.py")
        assert len(res) == 1
        assert res[0].evaluator.graph is mock_agent


def test_print_criteria_block_custom(cmd):
    cfg_match = CriterionConfig.tool_name_match(threshold=1.0)
    cfg_match.num_samples = 3
    cfg_match.judge_model = "gpt-4"
    cfg = EvalConfig(
        criteria=CriteriaConfig(
            tool_name_match=cfg_match,
        )
    )
    cmd._print_criteria_block(cfg, Path("some_dir"))
    rendered = "\n".join(cmd.output.infos)
    assert "samples=3" in rendered
    assert "judge=gpt-4" in rendered


def _pending_case(file_name, config, source):
    return _PendingCase(
        case=MagicMock(),
        evaluator=MagicMock(),
        config=config,
        file_name=file_name,
        eval_set_id="id",
        eval_set_name="name",
        config_source=source,
    )


def test_print_criteria_per_file_shows_each_files_criteria(cmd):
    tool_cfg = EvalConfig(
        criteria=CriteriaConfig(tool_name_match=CriterionConfig.tool_name_match(threshold=0.6))
    )
    rouge_cfg = EvalConfig(
        criteria=CriteriaConfig(rouge_match=CriterionConfig.rouge_match(threshold=0.8))
    )
    pending = [
        _pending_case("eval_tool_agents.py", tool_cfg, "per-file"),
        _pending_case("weather_agents_eval.py", rouge_cfg, "confeval.py"),
    ]
    cmd._print_criteria_per_file(pending, Path("evals/confeval.py"))
    out = "\n".join(cmd.output.infos)
    # Each file is listed with its own criteria and resolved source.
    assert "eval_tool_agents.py  (source: per-file)" in out
    assert "tool_name_match" in out
    assert f"weather_agents_eval.py  (source: {Path('evals/confeval.py')})" in out
    assert "rouge_match" in out


def test_criteria_by_file_maps_source_and_criteria(cmd):
    tool_cfg = EvalConfig(
        criteria=CriteriaConfig(tool_name_match=CriterionConfig.tool_name_match(threshold=0.6))
    )
    rouge_cfg = EvalConfig(
        criteria=CriteriaConfig(rouge_match=CriterionConfig.rouge_match(threshold=0.8))
    )
    pending = [
        _pending_case("eval_tool_agents.py", tool_cfg, "per-file"),
        _pending_case("weather_agents_eval.py", rouge_cfg, "confeval.py"),
    ]
    by_file = cmd._criteria_by_file(pending)
    assert by_file["eval_tool_agents.py"]["source"] == "per-file"
    assert "tool_name_match" in by_file["eval_tool_agents.py"]["criteria"]
    assert by_file["weather_agents_eval.py"]["source"] == "confeval.py"
    assert by_file["weather_agents_eval.py"]["criteria"]["rouge_match"]["threshold"] == 0.8


@pytest.mark.asyncio
async def test_run_flat_pool_simulation_no_criterion(cmd):
    from tenxgraph.qa.evaluation.token_usage import TokenUsage

    simulator = MagicMock()
    simulator.criteria = []

    sim_result = MagicMock()
    sim_result.completed = True
    sim_result.criterion_results = None
    sim_result.criterion_scores = {}
    sim_result.conversation = [{"role": "user", "content": "hello"}]
    sim_result.simulator_token_usage = TokenUsage(input_tokens=10, output_tokens=5)
    sim_result.turns = 2
    sim_result.goals_achieved = 1

    async def mock_simulator_run(graph, scenario):
        return sim_result

    simulator.run = mock_simulator_run

    ps = _PendingSimulation(
        scenario=MagicMock(scenario_id="sc1", description="sc_desc"),
        graph=MagicMock(),
        simulator=simulator,
        file_name="test_sim.py",
        eval_set_id="es_sim",
        eval_set_name="Sim Set",
    )

    results = await cmd._run_flat_pool([ps], max_concurrency=4, parallel=False)
    assert len(results) == 1
    assert len(results[0][3].criterion_results) == 1
    assert results[0][3].criterion_results[0].criterion == "simulation_completed"


@pytest.mark.asyncio
async def test_run_flat_pool_parallel(cmd):
    case1 = MagicMock()
    case1.eval_id = "c1"
    case1.name = "case_name"
    evaluator = MagicMock()

    async def mock_evaluate_case(case, collector_override):
        return EvalCaseResult.success(
            eval_id="c1",
            name="case_name",
            criterion_results=[],
            actual_response="resp",
        )

    evaluator._evaluate_case = mock_evaluate_case
    evaluator.collector.capture_all_events = True

    pc = _PendingCase(
        case=case1,
        evaluator=evaluator,
        config=EvalConfig(),
        file_name="test_file.py",
        eval_set_id="es1",
        eval_set_name="Eval Set 1",
    )

    with (
        patch("tenxgraph_api.cli.commands.eval._reset_inject_proxy"),
        patch("tenxgraph_api.cli.commands.eval.override_dependency"),
    ):
        results = await cmd._run_flat_pool([pc], max_concurrency=2, parallel=True)
        assert len(results) == 1
        assert results[0][3].passed is True


def test_resolve_eval_dir_error(cmd):
    mock_cm = MagicMock()
    mock_cm.auto_discover_config.return_value = "10xgraph.json"
    mock_cm.get_evaluation_config.side_effect = ValueError("config load error")

    with patch("tenxgraph_api.cli.commands.eval.ConfigManager", return_value=mock_cm):
        eval_dir = cmd._resolve_eval_dir()
        assert eval_dir == Path.cwd() / "evals"


def test_execute_load_config_error(cmd, tmp_path):
    mock_cm = MagicMock()
    mock_cm.auto_discover_config.return_value = "10xgraph.json"
    mock_cm.load_config.side_effect = ValueError("corrupt json")

    with (
        patch("tenxgraph_api.cli.commands.eval.ConfigManager", return_value=mock_cm),
        patch.object(cmd, "_discover", return_value=[]),
    ):
        code = cmd.execute(target=str(tmp_path))
        assert code == 1


def test_threshold_decides_the_exit_code():
    """A pass rate at or above --threshold exits 0 even when it is below 100 percent."""
    from tenxgraph_api.cli.commands.eval import EvalCommand

    assert EvalCommand._exit_code(0.8, 0.75) == 0
    assert EvalCommand._exit_code(0.75, 0.75) == 0
    assert EvalCommand._exit_code(0.7, 0.75) == 1
    assert EvalCommand._exit_code(0.9, None) == 1
    assert EvalCommand._exit_code(1.0, None) == 0
