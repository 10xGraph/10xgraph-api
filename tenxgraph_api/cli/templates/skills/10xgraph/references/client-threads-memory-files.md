# Client Threads, Memory, and Files

Use this when changing TypeScript APIs for thread management, checkpoint state/messages, memory store access, or file upload/multimodal helpers.

## Threads

Thread methods on `TenxGraphClient`:

- `threads(request?)`
- `threadDetails(threadId)`
- `deleteThread(threadId, config?)`
- `threadState(threadId)`
- `updateThreadState(threadId, config, state)`
- `clearThreadState(threadId)`
- `threadMessages(threadId, request?)`
- `addThreadMessages(threadId, messages, config?, metadata?)`
- `singleMessage(threadId, messageId)`
- `deleteMessage(threadId, messageId, config?)`

Request patterns:

- Thread listing supports search, offset, and limit.
- Thread message listing supports search, offset, and limit.
- Thread IDs may be string or number in most facade methods, but some endpoint signatures are narrower; check source before changing types.

## Memory

Memory methods:

- `storeMemory(request)`
- `searchMemory(request)`
- `getMemory(memoryId, options?)`
- `updateMemory(memoryId, content, options?)`
- `deleteMemory(memoryId, options?)`
- `listMemories(options?)`
- `forgetMemories(options?)`

Important enums/types:

- `MemoryType`
- `RetrievalStrategy`
- `DistanceMetric`
- memory result/response interfaces in endpoint files

The API must have a store configured for memory endpoints to work.

## Files and Multimodal

File methods:

- `uploadFile(file)`
- `getFile(fileId)`
- `getFileInfo(fileId)`
- `getFileAccessUrl(fileId)`
- `getMultimodalConfig()`

`uploadFile` accepts:

- Browser `File`
- `Blob`
- `{ data: Blob; filename: string }`

Use returned `file_id` in multimodal messages through `MediaRef(kind: "file_id", ...)` and content blocks such as `ImageBlock`, `DocumentBlock`, and `AudioBlock`.

## Rules

- Use stable `config.thread_id` for continuity; thread APIs inspect and mutate server-side checkpoints.
- Keep pagination options backwards-compatible.
- Keep memory enum values aligned with server store schemas.
- In browser code, ensure CORS and auth headers are configured before file or memory calls.
- Preserve MIME types and filenames when uploading files.
- Use `getFileAccessUrl` for fresh direct/signed URLs instead of caching old URLs indefinitely.

## Source Map

- Client facade: https://github.com/10xGraph/10xgraph-client/blob/master/src/client.ts
- Thread endpoints: https://github.com/10xGraph/10xgraph-client/tree/master/src/endpoints
- Memory endpoints: https://github.com/10xGraph/10xgraph-client/tree/master/src/endpoints
- File endpoint: https://github.com/10xGraph/10xgraph-client/blob/master/src/endpoints/files.ts
- Docs: https://10xgraph.com/
