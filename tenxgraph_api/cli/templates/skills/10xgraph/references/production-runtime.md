# Production Runtime

Use this when changing API serving, deployment, auth/middleware, worker scaling, environment config, or Docker generation.

## API Server

`10xgraph dev` starts a Uvicorn ASGI server (local development, with reload) with FastAPI. The server loads the configured compiled graph once at startup and reuses it for requests.

Runtime components:

- Uvicorn ASGI process.
- FastAPI app.
- Auth and permission middleware.
- Routers for graph, threads/checkpointer, store, ping, and files.
- `GraphService` for invoke/stream/stop/setup/fix operations.
- Configured checkpointer, store, media store, and DI container.

## Async Execution

The API runtime handles sync and async graph nodes. Blocking model calls should not block the FastAPI event loop; service/runtime code schedules appropriately.

## Multi-worker Rules

- Use durable/shared storage when multiple worker processes serve the same graph.
- `InMemoryCheckpointer` is process-local and unsuitable for load-balanced continuity.
- Use `PgCheckpointer` or equivalent durable backend for shared state.
- Use shared memory/media stores when clients can hit any worker.

## Environment Configuration

Important variables include:

- `MODE`: development or production.
- `LOG_LEVEL`
- `ORIGINS`
- `JWT_SECRET_KEY`
- `JWT_ALGORITHM`
- `REDIS_URL` for a `"cache_store": {"type": "redis"}` without its own `url` (a set `url` wins; `REDIS_URL` alone never selects Redis), and for user code such as `PgCheckpointer(redis_url=...)`
- `GRAPH_PATH`: path to `10xgraph.json`; defaults to `10xgraph.json`.

Production should set `MODE=production`, configure CORS origins, and use real auth secrets.

## CLI and Docker

- `10xgraph dev`: serve graph over HTTP and open the playground (`--no-open` to serve only).
- `10xgraph init`: scaffold config and graph.
- `10xgraph build --docker-compose`: generate Docker deployment files.

## Source Map

- App startup: https://github.com/10xGraph/10xgraph-api/blob/main/tenxgraph_api/src/app/main.py
- Route setup: https://github.com/10xGraph/10xgraph-api/blob/main/tenxgraph_api/src/app/routers/setup_router.py
- Graph service: https://github.com/10xGraph/10xgraph-api/tree/main/tenxgraph_api/src/app/routers/graph/services
- Config/middleware/auth: https://github.com/10xGraph/10xgraph-api/tree/main/tenxgraph_api/src/app/core
- CLI commands: https://github.com/10xGraph/10xgraph-api/tree/main/tenxgraph_api/cli
