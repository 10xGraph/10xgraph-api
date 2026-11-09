# ##[IF_POSTGRES]##
import os
# ##[/IF_POSTGRES]##
from datetime import datetime

from tenxgraph.core.state import MessageContextManager
from tenxgraph.prebuilt.agent import ReactAgent
# ##[IF_POSTGRES]##
from tenxgraph.storage.checkpointer import PgCheckpointer
# ##[/IF_POSTGRES]##
# ##[IF_SQLITE]##
from tenxgraph.storage.checkpointer import SqliteCheckpointer
# ##[/IF_SQLITE]##
# ##[IF_INMEM]##
from tenxgraph.storage.checkpointer import InMemoryCheckpointer
# ##[/IF_INMEM]##
# ##[IF_STORE]##
from tenxgraph.storage.store import PgVectorStore
# ##[/IF_STORE]##
# ##[IF_EMBED_GOOGLE]##
from tenxgraph.storage.store import GoogleEmbedding
# ##[/IF_EMBED_GOOGLE]##
# ##[IF_EMBED_OPENAI]##
from tenxgraph.storage.store import OpenAIEmbedding
# ##[/IF_EMBED_OPENAI]##
from dotenv import load_dotenv
from injectq import InjectQ

from graph.state import WeatherState
from graph.tools.weather_tool import get_weather
from graph.validators.manager import callback_manager


load_dotenv()

# Storage is chosen at init; run `10xgraph config` to review or change it.
# ##[IF_POSTGRES]##
# Durable thread state: Postgres keeps every thread, Redis caches the hot ones.
# Set DATABASE_URL and REDIS_URL in .env (see .env.example). The tables are created (or
# upgraded) when the server starts, so a fresh database needs no setup step.
checkpointer = PgCheckpointer(
    postgres_dsn=os.getenv("DATABASE_URL"),
    redis_url=os.getenv("REDIS_URL"),
)
# ##[/IF_POSTGRES]##
# ##[IF_SQLITE]##
# Durable thread state in a single local file. Fine for one process; use Postgres to run
# several workers.
checkpointer = SqliteCheckpointer(db_path="agent.db")
# ##[/IF_SQLITE]##
# ##[IF_INMEM]##
# Threads live only in this process: they are lost on restart and not shared between
# workers. Switch to a durable checkpointer before deploying.
checkpointer = InMemoryCheckpointer()
# ##[/IF_INMEM]##
# ##[IF_STORE]##

# Long-term memory in the same Postgres (pgvector).
store = PgVectorStore(
    # ##[IF_EMBED_GOOGLE]##
    embedding=GoogleEmbedding(),
    # ##[/IF_EMBED_GOOGLE]##
    # ##[IF_EMBED_OPENAI]##
    embedding=OpenAIEmbedding(),
    # ##[/IF_EMBED_OPENAI]##
    dsn=os.getenv("DATABASE_URL"),
)
# ##[/IF_STORE]##

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
    # ##[IF_STORE]##
    store=store,
    # ##[/IF_STORE]##
    callback_manager=callback_manager,
)
