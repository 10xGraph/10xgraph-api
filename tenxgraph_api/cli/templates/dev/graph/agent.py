import logging
from datetime import datetime
import zlib

from tenxgraph.core.state import AgentState, StreamEmitter
from tenxgraph.prebuilt.agent import ReactAgent
from tenxgraph.storage.checkpointer import InMemoryCheckpointer
from dotenv import load_dotenv


load_dotenv()

checkpointer = InMemoryCheckpointer()

logger = logging.getLogger(__name__)


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
    return f"The weather in {location} is {_weather_for(location)}"


def get_weather(
    location: str,
    tool_call_id: str | None = None,
    state: AgentState | None = None,
    emit: StreamEmitter | None = None,
) -> str:
    """Get the current weather for a specific location."""
    if tool_call_id:
        logger.info(f"Tool call ID: {tool_call_id}")
    if state and hasattr(state, "context"):
        logger.info(f"Number of messages in context: {len(state.context)}")  # type: ignore

    if emit:
        emit.progress(f"Fetching weather for {location}")

    return call_weather_api(location)


react_agent = ReactAgent(
    model="$llm_model",
    provider="$llm_provider",
    system_prompt=[
        {
            "role": "system",
            "content": """
                You are a helpful assistant.
                Your task is to assist the user in finding information and answering questions.
                Use tools when they help answer the user.
            """,
        },
        {
            "role": "user",
            "content": f"Today Date is {datetime.now().strftime('%Y-%m-%d')}",
        },  # Inject current date into system prompt
    ],
    trim_context=True,
    tools=[get_weather],
)


app = react_agent.compile(
    checkpointer=checkpointer,
)
