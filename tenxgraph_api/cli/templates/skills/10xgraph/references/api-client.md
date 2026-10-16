# API and TypeScript Client

Use this when aligning REST routes, schemas, generated docs, or the TypeScript npm SDK. The public npm package is `10xgraph-client`; install it with `npm install 10xgraph-client`.

Check current docs first: https://10xgraph.com/

## API Routers

Active routers are included from https://github.com/10xGraph/10xgraph-api/blob/main/tenxgraph_api/src/app/routers/setup_router.py:

- Graph router
- Checkpointer/thread router
- Store router
- Ping router
- Media/files router

Graph routes include:

- `POST /v1/graph/invoke`
- `POST /v1/graph/stream`
- `GET /v1/graph`
- `GET /v1/graph:StateSchema`
- `POST /v1/graph/stop`
- `POST /v1/graph/fix`

Memory/file routes are summarized in their topic references.

## TypeScript SDK Facade

`TenxGraphClient` is exported by `10xgraph-client`. Its source facade is https://github.com/10xGraph/10xgraph-client/blob/master/src/client.ts, and it wraps:

- Connectivity and metadata: `ping`, `graph`, `graphStateSchema`
- Execution: `invoke`, `stream`, `stopGraph`, `fixGraph`
- Threads/messages: `threadState`, `updateThreadState`, `clearThreadState`, `threadDetails`, `threads`, `threadMessages`, `addThreadMessages`, `singleMessage`, `deleteMessage`, `deleteThread`
- Memory: `storeMemory`, `searchMemory`, `getMemory`, `updateMemory`, `deleteMemory`, `listMemories`, `forgetMemories`
- Files: `uploadFile`, `getFile`, `getFileInfo`, `getFileAccessUrl`, `getMultimodalConfig`
- Remote tools: schemas in server `10xgraph.json`, handlers via `registerToolHandler`

For remote tools, read `remote-tools.md`; that flow has a client-managed execution loop around `remote_tool_call` blocks.

## Request Conventions

- The client serializes `Message` instances to plain API payloads.
- Server expects message content as an array of content blocks.
- `message_id` is sent as a string; `"0"` lets the server generate/normalize where applicable.
- `recursion_limit` defaults to `25`.
- Streaming defaults to low response granularity in the client.

## Rules

- Use `10xgraph-client` in user-facing examples and docs.
- The TypeScript SDK calls a running 10xGraph API server. It does not import or execute Python graph code.
- Keep endpoint schema changes mirrored in the client endpoints directory.
- Test both endpoint helpers and the `TenxGraphClient` facade for client-visible changes.

## Source Map

- Client facade: https://github.com/10xGraph/10xgraph-client/blob/master/src/client.ts
- Client endpoints: https://github.com/10xGraph/10xgraph-client/tree/master/src/endpoints
- Client message model: https://github.com/10xGraph/10xgraph-client/blob/master/src/message.ts
- API schemas/services: https://github.com/10xGraph/10xgraph-api/tree/main/tenxgraph_api/src/app/routers
