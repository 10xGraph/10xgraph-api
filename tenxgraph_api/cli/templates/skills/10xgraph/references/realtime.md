# Realtime audio-to-audio (Gemini Live)

Use this when building live, full-duplex audio (voice) agents. Unlike `invoke` / `stream`
(turn-based super-step traversal), a realtime graph is driven by a separate runtime because the
provider owns the turn loop. Provider neutrality is built in: contracts import no provider SDK, so
OpenAI Realtime can be added later behind the same `RealtimeClient` Protocol. Today only Gemini
Live is implemented.

## Install and environment

- `pip install "10xgraph[realtime]"` (pulls `google-genai>=1.56.0`).
- Set `GEMINI_API_KEY`; optionally `GEMINI_LIVE_MODEL` (default `gemini-live-2.5-flash-preview`).
- Provider SDK imports are lazy: importing `tenxgraph.core.realtime` never requires the extra.

## Audio and media formats

- Input audio: PCM16 mono @ 16 kHz. Output audio: PCM16 @ 24 kHz.
- Transcripts are persisted as `Message`s (`metadata={"modality": "audio"}`); raw audio is never
  stored.
- Image / video input: still images and video frames are sent live to the model as JPEG frames via
  `LiveInputQueue.send_image(...)`. Like ADK, there is no media store/offload in the realtime path;
  image frames are not persisted (a reconnect reseeds text transcripts only).

## Prebuilt `AudioAgent`

`from tenxgraph.prebuilt.agent import AudioAgent` — a React-style builder mirroring `ReactAgent`'s
construction surface, wrapping a `LiveAgent` as the graph root.

```python
from tenxgraph.prebuilt.agent import AudioAgent
from tenxgraph.core.realtime import RealtimeConfig

app = AudioAgent(
    "gemini-live-2.5-flash-preview",
    realtime_config=RealtimeConfig(model="gemini-live-2.5-flash-preview", voice="Puck"),
    tools=[my_tool],          # advertised to the model automatically; runs React-style
).compile()
```

- Constructor (positional): `model`, then optional `state`, `context_manager`, `publisher`,
  `id_generator`, `container`; keyword-only: `realtime_config`, `system_prompt`, `tools`, `client`,
  `pass_user_info_to_mcp`, `skills`, `memory`, `realtime_client_factory`, `live_node_name="LIVE"`.
- `compile()` takes `checkpointer`, `store`, `callback_manager`, `shutdown_timeout` (default 30.0).
  It does **not** take `media_store` or `interrupt_before` / `interrupt_after` — those belong to the
  turn-based super-step executor, which realtime bypasses.
- Tools work like a normal `ToolNode` (reason -> tool -> respond, including barge-in). No
  sub-agents / handoff in v1.
- `system_prompt`, `skills`, and `memory` work like `ReactAgent`: the agent's `system_prompt` (plus
  the skills catalog / session-mode skill content and the memory system prompt) is flattened
  into the single Gemini Live `system_instruction` at connect, and `{field}` placeholders are
  interpolated from state exactly like the turn-based path. Skill/memory **tools** are advertised
  normally. Caveat: `system_instruction` is fixed for the session, so state-dependent content
  (session-mode skill from a state field, memory preload) is a connect-time snapshot. Mid-session
  dynamism goes through `activate_skill` / memory tools, which work continuously.

`LiveAgent` (the graph root `AudioAgent` wraps) is at
`from tenxgraph.core.realtime.live_agent import LiveAgent`. It is not re-exported from
`tenxgraph.core.realtime`; import it from the module if you build the graph by hand.

## Driving a session: `CompiledGraph.arealtime` / `realtime`

- `arealtime(input_queue, config=None, state=None)` is an async generator yielding normalized
  `RealtimeEvent`s. `realtime(...)` is the sync wrapper (run with no active event loop).
- Forcing rule: the graph must contain exactly one `LiveAgent`; ordinary graphs raise. Conversely a
  graph containing a `LiveAgent` must use `arealtime()` — `invoke` / `stream` raise.

```python
from tenxgraph.core.realtime import LiveInputQueue

queue = LiveInputQueue()
queue.send_audio(pcm16_bytes)   # non-blocking; safe from an audio callback
async for event in app.arealtime(queue, {"thread_id": "t1"}):
    ...                         # AudioDeltaEvent / transcripts / ToolCallEvent / ...
queue.close()                   # ends the session once the provider goes idle
```

## Public API (`tenxgraph.core.realtime`)

- `LiveInputQueue` / `LiveInput` / `LiveInputKind` — non-blocking upstream input queue. Methods
  (all synchronous, callable from any context): `send_audio`, `send_text`, `send_image` (still
  image / video frame, default mime `image/jpeg`), `send_activity_start`, `send_activity_end`,
  `close`.
- `RealtimeConfig` — per-session config. Fields and defaults:
  - `model: str` (required)
  - `response_modalities: list[...] = ["AUDIO"]` (exactly one per session)
  - `voice: str | None = None`
  - `system_instruction: str | None = None`
  - `input_audio_transcription: bool = True`
  - `output_audio_transcription: bool = True`
  - `vad: VADConfig = VADConfig()`
  - `reconnect: ReconnectConfig = ReconnectConfig()`
  - `context_window_compression: bool = False`
  - `session_resumption: bool = True`
  - `tools: list | None = None`, `tools_tags: list[str] | None = None`
- `VADConfig` — voice-activity detection; disable for push-to-talk (manual activity via
  `send_activity_start` / `send_activity_end`).
- `ReconnectConfig` — reconnect/backoff for a dropped socket: `base_delay=0.5`, `max_delay=10.0`,
  `max_attempts=5` (set `0` to disable error-driven reconnect).
- `RealtimeEvent` — discriminated union (keyed on `type`): `AudioDeltaEvent`,
  `InputTranscriptEvent`, `OutputTranscriptEvent`, `ToolCallEvent`, `ToolResultEvent`,
  `TurnCompleteEvent`, `InterruptedEvent` (barge-in), `SessionUpdateEvent`, `GoAwayEvent`,
  `AgentChangedEvent`, `ErrorEvent`.
- `RealtimeClient` — provider Protocol (one implementation per provider).
- `GeminiLiveClient` / `normalize_message` — the Gemini Live provider client.

## Reconnection and resumption

Reconnect is automatic inside the realtime runtime (the builder / `AudioAgent` wires nothing).

- Provider `go_away` (planned rotation): reconnect immediately, no backoff.
- Transient drop / receive error: exponential backoff `min(base_delay * 2**(n-1), max_delay)`, up to
  `max_attempts`, then a fatal `ErrorEvent` (`code="reconnect_failed"`) ends the session.
- Tune per session via `RealtimeConfig.reconnect`:
  ```python
  from tenxgraph.core.realtime import RealtimeConfig, ReconnectConfig
  RealtimeConfig(model="...", reconnect=ReconnectConfig(base_delay=0.25, max_attempts=8))
  ```
- Context across reconnects: Gemini streams a resumption handle (`session_update`) that is persisted
  to checkpointer thread metadata; reconnect resumes provider-side context (requires
  `session_resumption=True`, the default). With no handle (a fresh session on the same `thread_id`),
  persisted transcript history is reseeded instead. Cross-session resume therefore needs a
  checkpointer.

## Session and turn lifecycle hooks

Realtime fires graph/turn hooks through the same `GraphLifecycleHook` used by turn-based graphs
(register via `CallbackManager.register_lifecycle_hook`, pass the manager to
`compile(callback_manager=...)`). These fire only in realtime (no-ops for `invoke` / `stream`):

- `on_graph_start(ctx, state)` / `on_graph_end(ctx, final_state, messages, total_steps)` — once per
  session (the `LIVE` node *is* the graph). `total_steps` = number of turns.
- `on_turn_start(ctx, state, turn_index)` / `on_turn_end(ctx, state, turn_index)` — per model turn
  (1-based; a turn spans one model generation, bounded by `turn_complete` or a barge-in). A turn cut
  off by session end still gets a balanced `on_turn_end`.

All hooks may return a modified state to replace the current one. Tool/MCP `before/after/error`
callbacks fire as usual (tools run through `ToolNode`). There is no `AI`-invocation callback or
input-validator pass in realtime (no discrete LLM call); `on_turn_start` / `on_turn_end` are the
per-turn observability stand-in.

## API server WebSocket bridge (`/v1/graph/live`)

`10xgraph dev` exposes `ws://<host>/v1/graph/live` when the configured graph is rooted at a
`LiveAgent`.

- First frame: a JSON control frame (e.g. `{"model": "...", "thread_id": "abc", "voice": "Puck"}`);
  present fields override the agent's build-time config for that session.
- Upstream: binary frame = PCM16 input audio; JSON control frame =
  `{"type": "text" | "activity_start" | "activity_end" | "close", ...}`. Image/video input is
  currently SDK-only via `LiveInputQueue.send_image`; the WebSocket bridge does not forward image
  frames yet.
- Downstream: binary frame = PCM16 model audio; JSON text frame = every other event (transcripts,
  `turn_complete`, `interrupted`, `tool_call`, session / `go_away`, `error`).

## Events / publisher additions

`Event.REALTIME` event and `ContentType.TRANSCRIPT` content type live in
`tenxgraph.runtime.publisher.events`.

## Examples

`examples/realtime/`: headless WAV-in/WAV-out (`audio_agent_file.py`), live full-duplex microphone
with React-style tool calling (`audio_agent_mic.py`), and the API WebSocket setup
(`10xgraph.json` + `graph.py`). See `examples/realtime/README.md`.

## Source Map

- Realtime package: https://github.com/10xGraph/10xGraph/tree/main/tenxgraph/core/realtime
- LiveAgent: https://github.com/10xGraph/10xGraph/blob/main/tenxgraph/core/realtime/live_agent.py
- AudioAgent: https://github.com/10xGraph/10xGraph/blob/main/tenxgraph/prebuilt/agent/audio.py
- Realtime drivers (`arealtime` / `realtime`): https://github.com/10xGraph/10xGraph/blob/main/tenxgraph/core/graph/compiled_graph.py
- WebSocket bridge: https://github.com/10xGraph/10xgraph-api/tree/main/tenxgraph_api/src/app/routers/graph
