# Client Messages, Invoke, and Stream

Use this when changing TypeScript message classes, invoke behavior, streaming behavior, response granularity, partial callbacks, or SSE parsing.

## Message Model

Message and content block classes live in https://github.com/10xGraph/10xgraph-client/blob/master/src/message.ts.

Core classes:

- `Message`
- `MediaRef`
- `AnnotationRef`
- `TokenUsages`

Content blocks:

- `TextBlock`
- `ImageBlock`
- `AudioBlock`
- `VideoBlock`
- `DocumentBlock`
- `DataBlock`
- `ToolCallBlock`
- `RemoteToolCallBlock`
- `ToolResultBlock`
- `ReasoningBlock`
- `AnnotationBlock`
- `ErrorBlock`

Message helpers:

- `Message.text_message(content, role?, message_id?)`
- `Message.tool_message(content, message_id?, meta?)`
- `message.text()`
- `message.attach_media(media, as_type)`
- `Message.withImage(text, imageUrl, altText?)`
- `Message.withFile(text, fileId, mimeType, filename?)`
- `Message.multimodal(text, mediaItems)`

The client serializes message content as arrays of content blocks. Avoid sending plain strings directly to API endpoints.

## Invoke

`TenxGraphClient.invoke(messages, options?)` wraps `/v1/graph/invoke`.

Options:

- `initial_state`
- `config`
- `recursion_limit`
- `response_granularity`: `"full"`, `"partial"`, or `"low"`
- `onPartialResult`

`InvokeResult` includes:

- `messages`
- `state`
- `context`
- `summary`
- `meta`
- `all_messages`
- `iterations`
- `recursion_limit_reached`

`onPartialResult` receives `InvokePartialResult` for every iteration, including `has_tool_calls` and `is_final`. Use it to observe remote tool loops or intermediate server results.

## Remote Tool Loop

Invoke automatically loops when server responses include `remote_tool_call` blocks and a `ToolExecutor` is available. The client executes registered handlers, sends tool result messages, and repeats until no remote calls remain or `recursion_limit` is reached.

Read `remote-tools.md` for the full remote tool flow.

## Stream

`TenxGraphClient.stream(messages, options?)` wraps `/v1/graph/stream` and returns `AsyncGenerator<StreamChunk>`.

`StreamEventType` values:

- `MESSAGE`
- `STATE`
- `ERROR`
- `UPDATES`

`StreamChunk` includes:

- `event`
- `message`
- `state`
- `data`
- `thread_id`
- `run_id`
- `metadata`
- `timestamp`

The stream endpoint parses server-sent event payloads and newline-delimited JSON variants. It also participates in the remote tool loop when remote tool calls appear.

Use `client.stopGraph(threadId, config?)` to request cancellation for a running stream.

## Rules

- Keep TypeScript `Message` block types aligned with Python `Message` and REST schemas.
- Use `response_granularity: "low"` for UI streaming unless state is needed.
- Surface `recursion_limit_reached` when remote tool loops do not finish.
- In React/browser code, consume streams with `for await`.
- Test both invoke and stream when changing message serialization.

## Source Map

- Message classes: https://github.com/10xGraph/10xgraph-client/blob/master/src/message.ts
- Client facade: https://github.com/10xGraph/10xgraph-client/blob/master/src/client.ts
- Invoke endpoint: https://github.com/10xGraph/10xgraph-client/blob/master/src/endpoints/invoke.ts
- Stream endpoint: https://github.com/10xGraph/10xgraph-client/blob/master/src/endpoints/stream.ts
- Stop graph endpoint: https://github.com/10xGraph/10xgraph-client/blob/master/src/endpoints/stopGraph.ts
- Docs: https://10xgraph.com/
