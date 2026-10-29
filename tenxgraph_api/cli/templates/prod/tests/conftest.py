from __future__ import annotations

import os


os.environ.setdefault("$llm_api_key_env", "test-key")
# PgCheckpointer connects lazily, so these only need to be well-formed to import the graph.
os.environ.setdefault("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/agent")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
