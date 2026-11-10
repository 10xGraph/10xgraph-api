from __future__ import annotations

import os


os.environ.setdefault("$llm_api_key_env", "test-key")
# ##[IF_POSTGRES]##
# PgCheckpointer connects lazily, so these only need to be well-formed to import the graph.
os.environ.setdefault("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/agent")
# ##[/IF_POSTGRES]##
# ##[IF_REDIS]##
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
# ##[/IF_REDIS]##
# ##[IF_EMBED_KEY]##
os.environ.setdefault("OPENAI_API_KEY", "test-key")
# ##[/IF_EMBED_KEY]##
