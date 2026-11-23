# Publishers and Runtime Protocols

Use this when adding observability events, external event buses, or runtime adapters.

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

Use adapters when translating provider-native formats into 10xGraph messages and tool schemas.

## A2A and ACP

There is no A2A or ACP support. Do not build on it or add it.

## Rules

- Prefer publisher events for observability instead of ad hoc print/log statements in reusable runtime paths.
- Keep publisher config serializable and environment-friendly.
- Close publishers on shutdown when they own network connections.
- Do not require optional protocol dependencies at core import time.

## Source Map

- Runtime exports: https://github.com/10xGraph/10xGraph/blob/main/tenxgraph/runtime/__init__.py
- Publishers: https://github.com/10xGraph/10xGraph/tree/main/tenxgraph/runtime/publisher
- Docs: https://10xgraph.com/
