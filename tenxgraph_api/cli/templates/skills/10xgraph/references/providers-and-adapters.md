# Providers and Adapters

Use this when changing OpenAI, Google Gemini, Vertex AI, reasoning, response conversion, or third-party tool adapters.

## Providers

`Agent` supports OpenAI and Google provider flows through provider-specific internal modules:

- OpenAI: https://github.com/10xGraph/10xGraph/blob/main/tenxgraph/core/graph/agent_internal/openai.py
- Google: https://github.com/10xGraph/10xGraph/blob/main/tenxgraph/core/graph/agent_internal/google.py
- Provider inference/helpers: https://github.com/10xGraph/10xGraph/blob/main/tenxgraph/core/graph/agent_internal/providers.py

Provider docs live in:

- Docs: https://10xgraph.com/

## Environment Variables

OpenAI:

- `OPENAI_API_KEY`

Anthropic:

- `ANTHROPIC_API_KEY`
- An unset key does not mean no credentials: the SDK also resolves
  `ANTHROPIC_AUTH_TOKEN`, an `ant auth login` profile, and Workload Identity
  Federation.
- Requires `pip install "10xgraph[anthropic]"`. For Claude on Vertex or
  Bedrock use the `anthropic-vertex` / `anthropic-bedrock` extra and pass
  `anthropic_backend="vertex"` or `"bedrock"` to the `Agent`.

Google Gemini API:

- `GEMINI_API_KEY`
- `GOOGLE_API_KEY` fallback

Vertex AI:

- `GOOGLE_GENAI_USE_VERTEXAI=true`
- `GOOGLE_CLOUD_PROJECT`
- `GOOGLE_CLOUD_LOCATION`
- `GOOGLE_APPLICATION_CREDENTIALS`

## Provider-specific settings

Pass provider settings to `Agent(...)` (or `ReactAgent(...)`, which forwards extra kwargs to `Agent`) as keyword arguments named exactly as the provider SDK names them:

- Anthropic: `cache_control`, `service_tier`, `inference_geo`, `metadata`
- OpenAI (Responses API, the default): `prompt_cache_key`, `prompt_cache_retention`, `service_tier`
- Google (`GenerateContentConfig` fields): `seed`, `safety_settings`, `cached_content`, `top_k`

10xGraph forwards a setting only if the SDK being called defines it: the method signature for Anthropic and OpenAI, the `GenerateContentConfig` fields for Google. Otherwise it is not sent and a warning names the key and provider. So one agent can carry settings for several providers (runtime model pick, fallback to another provider), and a new SDK parameter works after upgrading the SDK.

The check is by name only. If the SDK accepts the name but the API or model refuses the value (wrong value, unsupported feature, or a name two SDKs share with different meanings such as `metadata` or `service_tier`), the provider returns its error as usual.

Per model entry: with `model_registry`, put provider settings and endpoint keys in the entry of the model they belong to, and shared ones on the Agent (entry keys win). Names two SDKs share with different meanings (`service_tier`, `metadata`) belong in the entry. See "Model registry" in `agents-and-tools.md`.

## Anthropic prompt caching

On by default: a cache breakpoint on the last system block (or last tool) plus top-level automatic caching, so the whole conversation is cached and the breakpoint moves forward each call.

- `anthropic_cache=False` turns it off.
- `anthropic_cache={"type": "ephemeral", "ttl": "1h"}` sets a 1 hour TTL for both markers.
- If you set `cache_control` yourself (top level, on a system block, or on a tool), 10xGraph adds nothing and keeps yours.
- The minimum cacheable length depends on the model. Check `Message.usages.cache_read_input_tokens` (from the API's `usage.cache_read_input_tokens`).
- A cache write costs more than normal input (1.25x for 5 minutes); reads cost much less.

OpenAI and Gemini cache on the provider side; there is nothing to switch on.

## Reasoning

Reasoning config is provider-specific:

- OpenAI style: effort and summary options where supported.
- Google style: effort mapped to thinking budget or explicit `thinking_budget`.

Check provider capability and converter behavior before adding new reasoning fields.

## LLM Converters

Runtime converter exports:

- `BaseConverter`
- `GoogleGenAIConverter`
- `OpenAIConverter`
- `OpenAIResponsesConverter`
- `ModelResponseConverter`
- `reasoning_utils`

Converters normalize provider-native responses into 10xGraph `Message`, content blocks, usage, reasoning, and tool call structures.

## Tools

`ToolNode` runs local Python functions and MCP tools. There are no LangChain or Composio adapter classes in `tenxgraph`; wrap third-party tools in a local `@tool` function or expose them through an MCP server.

## Rules

- Use official provider SDKs through existing provider modules.
- Do not leak provider-native response shapes past converter boundaries unless stored in `Message.raw`.
- Keep optional provider/tool dependencies optional and guarded.
- Update provider docs and tests when adding provider-specific behavior.
- Verify multimodal and tool-call behavior for each provider separately.

## Source Map

- Agent internals: https://github.com/10xGraph/10xGraph/tree/main/tenxgraph/core/graph/agent_internal
- LLM adapters: https://github.com/10xGraph/10xGraph/tree/main/tenxgraph/runtime/adapters/llm
- Tool adapters: https://github.com/10xGraph/10xGraph/tree/main/tenxgraph/runtime/adapters/tools
- Docs: https://10xgraph.com/
