# ID and Thread Name Generators

Use this when changing API ID generation, Snowflake IDs, graph ID generators, or thread display-name generation.

## Python Graph ID Generators

Core graph ID generators live in `tenxgraph.utils`.

Built-ins:

- `DefaultIDGenerator`
- `UUIDGenerator`
- `BigIntIDGenerator`
- `TimestampIDGenerator`
- `IntIDGenerator`
- `HexIDGenerator`
- `ShortIDGenerator`
- `AsyncIDGenerator`

Pass to `StateGraph(id_generator=...)`, or name one in `10xgraph.json` (not both: the server
refuses to start when the graph is compiled with its own generator and the config sets one):

```json
{
  "id_generator": "bigint"
}
```

Names: `uuid`, `hex`, `short`, `int`, `bigint`, `timestamp`, `snowflake`, or a `module:attr`
import path to a `BaseIDGenerator` instance, subclass or zero-argument factory. Unset keeps the
graph's own generator. The server uses it for new thread ids, message ids and memory ids. With a
`PgCheckpointer`, its `id_type` must hold the ids (`id_type="bigint"` for integer generators,
`"string"` for the others); a mismatch refuses to start.

## API Snowflake ID Generator

The API package includes a Snowflake-style ID generator (`"id_generator": "snowflake"`, needs
`pip install "10xgraph-api[snowflakekit]"`). It reads these environment variables:

- `SNOWFLAKE_EPOCH`
- `SNOWFLAKE_NODE_ID`
- `SNOWFLAKE_WORKER_ID`
- `SNOWFLAKE_TIME_BITS`
- `SNOWFLAKE_NODE_BITS`
- `SNOWFLAKE_WORKER_BITS`

Use Snowflake IDs when deployment needs sortable, distributed, non-UUID identifiers.

## Thread Name Generator

Configure in `10xgraph.json`:

```json
{
  "thread_name_generator": "first_message"
}
```

- `"random"` (the default when unset): a random readable name such as `deep-dive`.
- `"first_message"`: the user's first message, whitespace collapsed, cut to at most 60
  characters on a word boundary; random when there is no text.
- `"none"`: new threads stay unnamed (the thread itself is saved).
- `"module:attr"`: your `ThreadNameGenerator` subclass or instance
  (`async def generate_name(self, messages: list[str]) -> str`). `messages` holds the user's
  message texts first, then the replies.

The name is generated after the first run on a new thread, in the background, and saved on the
thread (`GET /v1/threads`).

Keep thread name generation:

- Fast enough for request paths.
- Safe for arbitrary user messages.
- Deterministic enough for tests where needed.

## Rules

- Keep ID type expectations aligned across Python state, API responses, checkpointers, and TypeScript client types.
- For multi-worker deployments, avoid process-local counters unless partitioned safely.
- Validate Snowflake node/worker bit settings before production use.
- Treat thread names as display labels, not stable identifiers.

## Source Map

- Python ID generators: https://github.com/10xGraph/10xGraph/blob/main/tenxgraph/utils/id_generator.py
- API Snowflake generator: https://github.com/10xGraph/10xgraph-api/blob/main/tenxgraph_api/src/app/utils/snowflake_id_generator.py
- Thread name generator: https://github.com/10xGraph/10xgraph-api/blob/main/tenxgraph_api/src/app/utils/thread_name_generator.py
- Graph service thread naming: https://github.com/10xGraph/10xgraph-api/blob/main/tenxgraph_api/src/app/routers/graph/services/graph_service.py
- Settings: https://github.com/10xGraph/10xgraph-api/blob/main/tenxgraph_api/src/app/core/config/settings.py
- Docs: https://10xgraph.com/
