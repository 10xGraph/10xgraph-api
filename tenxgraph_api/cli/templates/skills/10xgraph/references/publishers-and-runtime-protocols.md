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

- Events and spans keep content on purpose. Redis, RabbitMQ and Kafka publishers send full events (message text, tool I/O); send them only to trusted systems. OTEL records prompts and tool I/O only at `ObservabilityLevel.FULL`.
- TLS is the developer's choice, never forced. Recommend it in production whenever possible, and always on networks the developer does not control; a broker reachable only inside a secured private network (private subnet or VPC, or an encrypting service mesh) can run without it. Redis: `rediss://` URL. RabbitMQ: `amqps://` URL. Kafka: opt-in config keys `security_protocol`, `ssl_context`, `ssl_cafile`, `ssl_certfile`, `ssl_keyfile`, `sasl_mechanism`, `sasl_plain_username`, `sasl_plain_password` (passed to aiokafka only when set; `ssl_context` wins over the file keys).
- `user_id_mode` (`"raw"` default, `"hash"`, `"mask"`) controls `user_id` on OTEL spans, and so on Logfire and LangSmith. It does not apply to Redis, RabbitMQ, Kafka or Console events. `hash` is the first 16 hex chars of SHA-256, or HMAC-SHA256 when env `TENXGRAPH_TRACE_USER_ID_KEY` is set (set it; plain SHA-256 of short ids can be guessed). Options: `OtelPublisher(level=..., user_id_mode=...)`, `setup_tracing`, `setup_logfire`, `setup_langsmith`, `setup_observability(graph, {"user_id_mode": ...})`. Invalid value raises `ValueError`.
- Server: `10xgraph.json` `"observability": {"user_id_mode": "hash"}`; also in the `10xgraph config` editor.

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
