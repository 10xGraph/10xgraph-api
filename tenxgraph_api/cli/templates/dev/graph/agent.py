import logging
from datetime import datetime

from tenxgraph.core.state import AgentState, StreamEmitter
from tenxgraph.prebuilt.agent import ReactAgent
from tenxgraph.storage.checkpointer import InMemoryCheckpointer
from dotenv import load_dotenv


load_dotenv()

checkpointer = InMemoryCheckpointer()

logger = logging.getLogger(__name__)


def call_weather_api(location: str) -> str:
    return f"The weather in {location} is sunny"


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
