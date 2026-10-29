import os
from datetime import datetime

from tenxgraph.core.state import MessageContextManager
from tenxgraph.prebuilt.agent import ReactAgent
from tenxgraph.storage.checkpointer import PgCheckpointer
from dotenv import load_dotenv
from injectq import InjectQ

from graph.state import WeatherState
from graph.tools.weather_tool import get_weather
from graph.validators.manager import callback_manager


load_dotenv()

# Durable thread state: Postgres keeps every thread, Redis caches the hot ones.
# Set DATABASE_URL and REDIS_URL in .env (see .env.example).
checkpointer = PgCheckpointer(
    postgres_dsn=os.getenv("DATABASE_URL"),
    redis_url=os.getenv("REDIS_URL"),
)

# Local testing without Postgres and Redis: comment out the block above and use this.
# Threads live only in this process (lost on restart, not shared between workers),
# so switch back to PgCheckpointer once you are ready to deploy.
# from tenxgraph.storage.checkpointer import InMemoryCheckpointer
# checkpointer = InMemoryCheckpointer()

# Dependency injection container; 10xgraph.json points at it ("injectq": "graph.agent:container").
container = InjectQ.get_instance()

context_manager = MessageContextManager(
    max_messages=20,  # last 20 user messages will be kept in context
    remove_tool_msgs=True,
)

react_agent = ReactAgent(
    state=WeatherState(),
    model="$llm_model",
    provider="$llm_provider",
    system_prompt=[
        {
            "role": "system",
            "content": """
                You are a helpful assistant.
                Your task is to assist the user in finding information and answering questions.
                User Current Location: {user_location}
                If missing information, ask the user for clarification.
                Use tools when they help answer the user.
            """,
        },
        {
            "role": "user",
            "content": f"Today Date is {datetime.now().strftime('%Y-%m-%d')}",
        },
    ],
    trim_context=True,
    tools=[get_weather],
    context_manager=context_manager,
    container=container,
)

app = react_agent.compile(
    checkpointer=checkpointer,
    callback_manager=callback_manager,
)
