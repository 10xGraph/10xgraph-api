# Publishers and Runtime Protocols

Use this when adding observability events, external event buses, A2A integration, ACP, or runtime adapters.

## Publishers

Publisher exports live under `tenxgraph.runtime.publisher` and `tenxgraph.runtime`.

Core types:

- `EventModel`: event payload model.
- `Event`: event source enum.
- `EventType`: lifecycle/phase enum.
- `ContentType`: payload content enum.
- `BasePublisher`: abstract publisher interface.
- `publish_event`: helper used by runtime internals.

Implementations:

- `ConsolePublisher`
- `RedisPublisher`
- `KafkaPublisher`
- `RabbitMQPublisher`

Publishers receive structured events from graph/tool execution. Use them for tracing, monitoring, audit logs, and external streaming/event bus integrations.

`ConsolePublisher` is a dev/debug, opt-in publisher (use a real transport in production). It writes
to stdout by default; pass `ConsolePublisher({"use_logger": True})` to route events through the
`tenxgraph.publisher` logger instead of stdout.

Publisher kinds:

- `BasePublisher.kind` is `"events"` by default (type `PublisherKind = Literal["events", "observability"]`). `OtelPublisher` sets `"observability"`, so `LogfirePublisher`, `LangsmithPublisher` and `PhoenixPublisher` have it too. A custom tracing backend sets `kind = "observability"`.
- `publish_event(event, only="events")` sends to event publishers only; `only="observability"` to tracing publishers only; `None` (default) to all. It works inside a `CompositePublisher`, also nested.
- Event publishers (Kafka, Redis, RabbitMQ, console, custom) are bound in code; there is no `10xgraph.json` key. Tracing publishers come from the `observability` block. `metrics` never goes through publishers.
- Routing: graph, node, LLM and tool events (AI errors included) go to event publishers and tracing, plus logs and LLM metrics. Task lifecycle events (queued, started, finished, saved, cancelled) go to event publishers only (`only="events"`), plus task metrics and logs. A task failure is not published: ERROR log and `tenxgraph.task.runs{status="failure"}`. API HTTP errors are not published: HTTP metrics and logs.

Realtime adds an `Event.REALTIME` event source and a `ContentType.TRANSCRIPT` content type (see
`realtime.md`).

## Runtime Adapters

LLM adapters:

- `BaseConverter`
- `GoogleGenAIConverter`
- `OpenAIConverter`
- `OpenAIResponsesConverter`
- `ConverterType`

Tool adapters:

- `LangChainAdapter`
- `ComposioAdapter`

Use adapters when translating provider-native or third-party tool formats into 10xGraph messages, tool schemas, and execution results.

## A2A Runtime

A2A helpers live in `tenxgraph.runtime.protocols.a2a`.

Key helpers:

- `make_agent_card`
- `build_a2a_app`
- `create_a2a_server`
- `delegate_to_a2a_agent`
- `create_a2a_client_node`
- `AgentFlowExecutor`

The A2A extras require the optional `a2a-sdk` dependency. Keep imports lazy or guarded where the dependency may not be installed.

## ACP

ACP support is in `tenxgraph/runtime/protocols/acp.py`. Treat it as a runtime protocol surface and check source before extending because public docs are thinner than core graph docs.

## Content, TLS and user ids

- Events and spans keep content on purpose. Redis, RabbitMQ and Kafka publishers send full events (message text, tool I/O); send them only to trusted systems. The OTEL level defaults to `ObservabilityLevel.FULL` (`"level": "full"`, `OTEL_LEVEL=full`), which sends message content, tool I/O and graph state to the trace backend; set `"standard"` or `"spans"` to keep content out.
- TLS is the developer's choice, never forced. Recommend it in production whenever possible, and always on networks the developer does not control; a broker reachable only inside a secured private network (private subnet or VPC, or an encrypting service mesh) can run without it. Redis: `rediss://` URL. RabbitMQ: `amqps://` URL. Kafka: opt-in config keys `security_protocol`, `ssl_context`, `ssl_cafile`, `ssl_certfile`, `ssl_keyfile`, `sasl_mechanism`, `sasl_plain_username`, `sasl_plain_password` (passed to aiokafka only when set; `ssl_context` wins over the file keys).
- `user_id_mode` (`"raw"` default, `"hash"`, `"mask"`) controls `user_id` on OTEL spans, and so on Logfire, LangSmith and Phoenix. It does not apply to Redis, RabbitMQ, Kafka or Console events. `hash` is the first 16 hex chars of SHA-256, or HMAC-SHA256 when env `TENXGRAPH_TRACE_USER_ID_KEY` is set (set it; plain SHA-256 of short ids can be guessed). Options: `OtelPublisher(level=..., user_id_mode=...)`, `setup_tracing`, `setup_logfire`, `setup_langsmith`, `setup_observability(graph, {"user_id_mode": ...})`. Invalid value raises `ValueError`.
- Server: `10xgraph.json` `"observability": {"user_id_mode": "hash"}`; also in the `10xgraph config` editor.

## Phoenix (Arize)

- Install `pip install '10xgraph[phoenix]'` (also in `10xgraph[observability]`). Enable in `10xgraph.json`: `"observability": {"phoenix": {"enabled": true, "project": "my-agent", "endpoint": null}}`.
- `project` falls back to env `PHOENIX_PROJECT` / `PHOENIX_PROJECT_NAME`; `endpoint` is the base collector URL (env `PHOENIX_COLLECTOR_ENDPOINT`, then `http://localhost:6006`), `/v1/traces` is appended.
- In Python: `setup_phoenix(graph, project=..., endpoint=..., api_key=...)` or `PhoenixPublisher(...)`, both from `tenxgraph.runtime.publisher`, used like `setup_langsmith` / `LangsmithPublisher`. Call before `graph.compile()`.
- `PHOENIX_API_KEY` is env only and optional (a local Phoenix needs none). Never put it in the config file.

## OTLP tracing and the metrics block

- AI traces to any OTLP HTTP receiver (collector, Tempo, Jaeger, Grafana Cloud; not Prometheus): install `pip install '10xgraph[otlp]'`, config `"observability": {"otlp": {"enabled": true, "endpoint": "http://collector:4318"}}`. `/v1/traces` is appended. With a null endpoint, env `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` is required (`OTEL_EXPORTER_OTLP_ENDPOINT` is not used). In Python: `setup_otlp(graph, endpoint=...)` before `graph.compile()`. Auth headers: env `OTEL_EXPORTER_OTLP_HEADERS` only.
- `observability.otlp.use_metrics_endpoint: true` reuses `metrics.otlp.endpoint` (needs metrics on with `export: "otlp"`). The URL must take traces; a Prometheus `/api/v1/otlp` URL does not.
- HTTP request spans never go to AI tracing; the server turns FastAPI's own telemetry off.
- Metrics are separate from tracing: `"metrics": {"enabled": true, "export": "otlp" | "prometheus", "otlp": {"endpoint": ...}, "prometheus": {"path": "/metrics", "worker_port": 9464}, "latency_buckets_ms": [...]}`. Needs `pip install '10xgraph-api[metrics]'`. Prometheus scrape covers one process (API route, worker port); with several gunicorn or TaskIQ processes use OTLP push. The scrape route has no auth, keep it on a private network.
- Core metrics include `tenxgraph.llm.duration` (provider, model, status) and `tenxgraph.llm.tokens` (provider, model, type); the task metrics are `tenxgraph.task.runs` and `tenxgraph.task.duration`.

## Rules

- Prefer publisher events for observability instead of ad hoc print/log statements in reusable runtime paths.
- Keep publisher config serializable and environment-friendly.
- Close publishers on shutdown when they own network connections.
- Do not require optional protocol dependencies at core import time.
- For A2A, distinguish serving a 10xGraph graph as an A2A app from delegating to another A2A agent.

## Source Map

- Runtime exports: https://github.com/10xGraph/10xGraph/blob/main/tenxgraph/runtime/__init__.py
- Publishers: https://github.com/10xGraph/10xGraph/tree/main/tenxgraph/runtime/publisher
- A2A protocol: https://github.com/10xGraph/10xGraph/tree/main/tenxgraph/runtime/protocols/a2a
- ACP protocol: https://github.com/10xGraph/10xGraph/blob/main/tenxgraph/runtime/protocols/acp.py
- Docs: https://10xgraph.com/
