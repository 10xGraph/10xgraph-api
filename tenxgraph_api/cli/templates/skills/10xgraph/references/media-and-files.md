# Media and Files

Use this when implementing multimodal messages, file upload/download, media stores, or provider media handling.

## Message Model

Messages can mix text and media blocks. Media bytes are referenced through `MediaRef` rather than stored directly in graph logic.

`MediaRef.kind` values:

- `"url"`: external URL.
- `"data"`: inline base64 bytes for small payloads.
- `"file_id"`: key returned by a media store or upload route.

Content blocks include image, audio, video, document, and generic data blocks. See `state-and-messages.md` for the full list.

## Media Stores

Media stores keep binary data outside messages:

- `InMemoryMediaStore`: tests/dev only.
- `LocalFileMediaStore`: local single-server storage with metadata sidecars.
- `CloudMediaStore`: cloud object storage with signed URL support where configured.

The base store supports storing, retrieving, deleting, existence checks, and metadata reads.

## MultimodalConfig

Use `MultimodalConfig` on `Agent` to control provider transport:

- Image handling: base64, URL, or provider file ID.
- Document handling: extract text, forward raw, or skip.
- Size/dimension/type constraints for media safety and provider compatibility.

The resolver should pick a supported transport based on provider/model capabilities.

## REST and Client

API routes:

- `POST /v1/files/upload`
- `GET /v1/files/{file_id}`
- `GET /v1/files/{file_id}/info`
- `GET /v1/files/{file_id}/url`
- `GET /v1/config/multimodal`

TypeScript client methods:

- `uploadFile`
- `getFile`
- `getFileInfo`
- `getFileAccessUrl`
- `getMultimodalConfig`

## Rules

- Use `file_id` for repeated or large media.
- Inline base64 only for small payloads.
- Preserve MIME types.
- Pass `media_store` at compile/runtime boundaries where the graph must dereference `file_id` media.

## Source Map

- Media config/resolver: https://github.com/10xGraph/10xGraph/tree/main/tenxgraph/storage/media
- Media router: https://github.com/10xGraph/10xgraph-api/tree/main/tenxgraph_api/src/app/routers/media
- TS files endpoint: https://github.com/10xGraph/10xgraph-client/blob/master/src/endpoints/files.ts
