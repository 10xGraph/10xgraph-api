"""The scaffolded eval files check the expectations they declare and are satisfiable."""

import importlib.util
from pathlib import Path

import tenxgraph_api


EVALS = Path(tenxgraph_api.__file__).parent / "cli" / "templates" / "prod" / "evals"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, EVALS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_weather_eval_checks_response_tools_and_node_order() -> None:
    module = _load("weather_agents_eval")
    criteria = module.get_eval_config().criteria
    assert criteria.tool_name_match is not None
    assert criteria.trajectory is not None
    assert criteria.rouge_match is not None
    assert criteria.node_order is not None
    cases = module.get_eval_set().eval_cases
    for case in cases:
        assert case.expected_summary()["response"] != "assistant"


def test_simulator_scenarios_fit_a_weather_only_agent() -> None:
    module = _load("user_simulator_eval")
    ids = [s.scenario_id for s in module.get_scenarios()]
    assert "flight_booking_assistance" not in ids
    assert len(ids) == 2
