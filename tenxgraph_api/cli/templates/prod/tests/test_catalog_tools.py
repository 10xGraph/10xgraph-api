from __future__ import annotations

from unittest.mock import MagicMock, patch

from graph.state import WeatherState
from graph.tools.weather_tool import get_weather


def test_get_weather_returns_sunny_message_for_location() -> None:
    with patch(
        "graph.tools.weather_tool.call_weather_api", return_value="The weather in London is sunny"
    ):
        result = get_weather(location="London")

    assert "London" in result
    assert "sunny" in result


def test_get_weather_retries_on_failure_and_returns_error_after_max_attempts() -> None:
    with patch("graph.tools.weather_tool.call_weather_api", side_effect=Exception("API error")):
        result = get_weather(location="Paris")

    assert "Paris" in result
    assert "sorry" in result.lower()


def test_get_weather_emits_progress_events_when_emitter_is_provided() -> None:
    emit = MagicMock()

    with patch(
        "graph.tools.weather_tool.call_weather_api", return_value="The weather in Tokyo is sunny"
    ):
        result = get_weather(location="Tokyo", emit=emit)

    emit.progress.assert_called()
    assert "Tokyo" in result


def test_get_weather_accepts_injected_tool_call_id_and_state() -> None:
    state = WeatherState()

    with patch(
        "graph.tools.weather_tool.call_weather_api", return_value="The weather in Berlin is sunny"
    ):
        result = get_weather(location="Berlin", tool_call_id="call_1", state=state)

    assert "Berlin" in result


def test_weather_is_deterministic_and_differs_between_cities() -> None:
    from graph.tools.weather_tool import _weather_for

    assert _weather_for("Rome") == _weather_for("rome ")
    assert _weather_for("Rome") != _weather_for("Berlin")
    assert _weather_for("Atlantis") == _weather_for("Atlantis")
