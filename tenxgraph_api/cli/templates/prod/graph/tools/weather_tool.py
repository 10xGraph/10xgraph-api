import logging
import random
import zlib

from tenxgraph.core.state.stream_emitter import StreamEmitter

from graph.state import WeatherState


logger = logging.getLogger(__name__)

MAX_RETRIES = 3


_CITY_WEATHER = {
    "london": "cloudy, 14C",
    "new york": "partly cloudy, 18C",
    "tokyo": "sunny, 24C",
    "paris": "rainy, 12C",
    "rome": "sunny, 27C",
    "berlin": "overcast, 10C",
    "sydney": "clear, 22C",
    "cairo": "hot and dry, 35C",
}
_FALLBACK_WEATHER = ("sunny, 25C", "cloudy, 15C", "rainy, 11C", "windy, 17C", "foggy, 9C")


def _weather_for(location: str) -> str:
    """Deterministic weather: known cities from a table, others from a stable hash."""
    key = location.strip().lower()
    if key in _CITY_WEATHER:
        return _CITY_WEATHER[key]
    return _FALLBACK_WEATHER[zlib.crc32(key.encode("utf-8")) % len(_FALLBACK_WEATHER)]


def call_weather_api(location: str) -> str:
    is_failed = random.choice([True, False])  # noqa: S311
    if is_failed:
        raise Exception("Failed to fetch weather data due to a simulated API error.")
    return f"The weather in {location} is {_weather_for(location)}"


def get_weather(
    location: str,
    tool_call_id: str | None = None,
    state: WeatherState | None = None,
    emit: StreamEmitter | None = None,
) -> str:
    """Get the current weather for a specific location."""
    if tool_call_id:
        logger.info(f"Tool call ID: {tool_call_id}")
    if state and hasattr(state, "context"):
        logger.info(f"Number of messages in context: {len(state.context)}")  # type: ignore
    if emit:
        emit.progress("Fetching weather data...")

    for i in range(MAX_RETRIES):
        try:
            return call_weather_api(location)
        except Exception as e:
            logger.error(f"Attempt {i + 1} failed: {e}")
            if emit:
                emit.progress(f"Attempt {i + 1} failed, retrying...")

    return f"Sorry, I couldn't fetch the weather for {location} after multiple attempts."
