# State and Messages

Use this when changing state schemas, message serialization, multimodal blocks, tool call/result blocks, context trimming, or API/client message compatibility.

## AgentState

Default state fields:

- `context`: list of `Message`, using the `add_messages` reducer.
- `context_summary`: optional summary for trimmed context.
- `execution_meta`: runtime metadata such as current node, step count, interrupt, error, and stop state.

Extend `AgentState` for app fields:

```python
from tenxgraph.core.state import AgentState

class OrderState(AgentState):
    order_id: str = ""
    total: float = 0.0
```

Only returned dict keys are merged. Returning a `Message` appends to `context`; it does not replace history.

## Message

`Message` represents one conversation turn. Important fields:

- `message_id`
- `role`: `"user"`, `"assistant"`, `"system"`, or `"tool"`
- `content`: list of typed content blocks
- `delta`: true for partial/streaming messages
- `tools_calls`: provider-native tool call list
- `reasoning`, `timestamp`, `metadata`, `usages`, `raw`

Use `Message.text_message("...", role="user")` for plain text. Use `msg.text()` to concatenate `TextBlock` content.

## Content Blocks

Supported block families:

- `TextBlock`: text and annotations.
- `ImageBlock`: image plus `MediaRef`.
- `AudioBlock`: audio plus transcript/hints.
- `VideoBlock`: video plus thumbnail.
- `DocumentBlock`: document media plus optional extracted text/pages/excerpt.
- `DataBlock`: generic binary data.
- `ToolCallBlock`: model-requested tool call.
- `ToolResultBlock`: tool output paired by call ID.
- `ReasoningBlock`: provider reasoning trace.
- `AnnotationBlock`: citations/references.
- `ErrorBlock`: structured error data.

## ToolResult

Use `ToolResult(message=..., state={...})` when a tool must both return text to the model and mutate state. Only the named state fields are updated.

## Context Trimming

`Agent(trim_context=True)` trims what is sent to the model and writes a summary to `context_summary`. The checkpointer should still preserve full history.

## Source Map

- State models: https://github.com/10xGraph/10xGraph/blob/main/tenxgraph/core/state/agent_state.py
- Message model: https://github.com/10xGraph/10xGraph/blob/main/tenxgraph/core/state/message.py
- Content blocks: https://github.com/10xGraph/10xGraph/blob/main/tenxgraph/core/state/message_block.py
- Reducers: https://github.com/10xGraph/10xGraph/blob/main/tenxgraph/core/state/reducers.py
- TS message class: https://github.com/10xGraph/10xgraph-client/blob/master/src/message.ts
