from __future__ import annotations

from graph.agent import app, container, react_agent


def test_main_agent_is_configured_for_weather_assistant() -> None:
    agent = react_agent._agent
    assert agent.model == "$llm_model"
    assert agent.provider == "$llm_provider"
    assert agent.trim_context is True

    system_prompt_text = " ".join(
        block["content"] for block in agent.system_prompt if "content" in block
    )
    assert "helpful assistant" in system_prompt_text.lower()


def test_graph_compiles() -> None:
    assert app is not None


def test_container_is_exported_for_10xgraph_json() -> None:
    # 10xgraph.json loads "graph.agent:container"; the server refuses to start without it.
    assert container is not None
