"""Run-trace capture and reconstruction for the observability endpoint.

``telemetry_record`` flattens one ``StreamChunk`` into a compact, JSON-able record that the
in-memory ``TelemetryStore`` keeps. ``reconstruct_run`` turns the records of one run into a
span tree (root > node visit > llm / tool call) with real offsets, durations, status, and
inputs/outputs where the recorded chunks contain them.

What the stream does not carry is not invented: the LLM prompt is never recorded, node inputs
are only the latest message seen before the visit, and ``invoke`` (non-stream) runs have no
per-node events.
"""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING, Any


if TYPE_CHECKING:
    from tenxgraph_api.src.app.routers.graph.schemas.graph_schemas import (
        ObsRunSchema,
        ObsSpanSchema,
    )


CLIP_CHARS = 4000
MAX_RECORDS = 2000
MS_PER_SECOND = 1000.0
_NO_MODEL = {"", "unknown", "none"}

_STATUS_MAP = {"done": "ok", "error": "error", "running": "running", "stopped": "ok"}


def _get(obj: Any, key: str) -> Any:
    if isinstance(obj, dict):
        return obj.get(key)
    return getattr(obj, key, None)


def clip(value: Any) -> Any:
    """Return a JSON-able copy of ``value``, clipped to ``CLIP_CHARS`` characters."""
    if value is None or isinstance(value, bool | int | float):
        return value
    try:
        text = value if isinstance(value, str) else json.dumps(value, default=str)
    except (TypeError, ValueError):
        text = str(value)
    if len(text) <= CLIP_CHARS:
        if isinstance(value, str):
            return value
        try:
            return json.loads(text)
        except ValueError:
            return text
    return f"{text[:CLIP_CHARS]}...[truncated {len(text) - CLIP_CHARS} chars]"


def _usage_dict(message: Any) -> dict[str, Any] | None:
    usages = getattr(message, "usages", None)
    if usages is None:
        return None
    try:
        return usages.model_dump() if hasattr(usages, "model_dump") else dict(usages)
    except Exception:
        return None


def _model_name(message: Any) -> str | None:
    meta = getattr(message, "metadata", None)
    if not isinstance(meta, dict):
        return None
    name = meta.get("model")
    if isinstance(name, str) and name.strip().lower() not in _NO_MODEL:
        return name
    return None


def telemetry_record(chunk: Any, ts: float | None = None) -> dict[str, Any]:
    """Flatten a StreamChunk into a compact record for the telemetry store.

    ``ts`` is the arrival time. ``StreamChunk.timestamp`` is stamped once at import by core,
    so it cannot order chunks; the caller passes the real clock instead.
    """
    md = getattr(chunk, "metadata", None) or {}
    data = getattr(chunk, "data", None)
    data = data if isinstance(data, dict) else {}
    node = (
        md.get("node")
        or md.get("current_node")
        or data.get("node")
        or getattr(chunk, "node_name", "")
        or ""
    )
    step = data.get("step", md.get("step"))

    message = getattr(chunk, "message", None)
    content_kinds: list[str] = []
    tool_names: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    tool_results: list[dict[str, Any]] = []
    texts: list[str] = []
    block_errors: list[str] = []
    if message is not None:
        content = getattr(message, "content", None)
        if isinstance(content, list):
            for block in content:
                btype = _get(block, "type")
                if btype:
                    content_kinds.append(btype)
                if btype == "text" and _get(block, "text"):
                    texts.append(str(_get(block, "text")))
                elif btype == "tool_call":
                    name = _get(block, "name")
                    if name:
                        tool_names.append(name)
                    tool_calls.append(
                        {
                            "id": _get(block, "id"),
                            "name": name,
                            "args": clip(_get(block, "args") or {}),
                        }
                    )
                elif btype == "tool_result":
                    name = _get(block, "name")
                    if name:
                        tool_names.append(name)
                    tool_results.append(
                        {
                            "call_id": _get(block, "call_id"),
                            "output": clip(_get(block, "output")),
                            "is_error": bool(_get(block, "is_error")),
                        }
                    )
                elif btype == "error" and _get(block, "message"):
                    block_errors.append(str(_get(block, "message")))

    event: Any = getattr(chunk, "event", None)
    event_name = str(event.value) if hasattr(event, "value") else str(event or "")
    is_error = event_name == "error" or bool(getattr(chunk, "is_error", False))
    error = None
    if is_error or block_errors:
        error = clip(
            data.get("error")
            or data.get("reason")
            or data.get("message")
            or "; ".join(block_errors)
        )
        error = str(error) if error else None

    text = "\n".join(texts)
    return {
        "event": event_name,
        "node": node,
        "step": step if isinstance(step, int) else None,
        "timestamp": ts if ts is not None else time.time(),
        "is_delta": bool(getattr(message, "delta", False)) if message else False,
        "role": getattr(message, "role", None) if message else None,
        "message_id": str(getattr(message, "message_id", "") or "") if message else "",
        "usages": _usage_dict(message) if message is not None else None,
        "content_kinds": content_kinds,
        "tool_names": tool_names,
        "is_error": is_error,
        "status": data.get("status"),
        "model": _model_name(message) if message is not None else None,
        "text": clip(text) if text else None,
        "tool_calls": tool_calls,
        "tool_results": tool_results,
        "tool_call_id": data.get("tool_call_id"),
        "tool_name": data.get("tool_name"),
        "args": clip(data.get("args")) if data.get("args") is not None else None,
        "error": error,
    }


def event_summary(rec: dict[str, Any]) -> str:
    kinds = rec.get("content_kinds") or []
    tools = rec.get("tool_names") or []
    if rec.get("is_error"):
        return "error"
    if tools and ("tool_call" in kinds or "tool_result" in kinds):
        label = "tool_call" if "tool_call" in kinds else "tool_result"
        return f"{label} {', '.join(tools)}"
    if rec.get("usages"):
        u = rec["usages"]
        return f"llm.generate · {u.get('total_tokens', 0)} tokens"
    if rec.get("is_delta"):
        return "delta"
    if kinds:
        return f"{rec.get('role') or 'message'}: {', '.join(kinds)}"
    return rec.get("event") or "chunk"


def _summarize_message(rec: dict[str, Any]) -> dict[str, Any]:
    summary: dict[str, Any] = {"role": rec.get("role")}
    if rec.get("text"):
        summary["text"] = rec["text"]
    if rec.get("tool_calls"):
        summary["tool_calls"] = [
            {"id": c.get("id"), "name": c.get("name")} for c in rec["tool_calls"]
        ]
    if rec.get("tool_results"):
        summary["tool_results"] = [
            {"call_id": r.get("call_id"), "is_error": r.get("is_error", False)}
            for r in rec["tool_results"]
        ]
    return summary


class _Visit:
    def __init__(self, span: ObsSpanSchema, node: str, step: int | None, ts: float) -> None:
        self.span = span
        self.node = node
        self.step = step
        self.last_ts = ts
        self.boundary = ts
        self.messages: list[dict[str, Any]] = []
        self.seen_ids: set[str] = set()
        self.error: str | None = None
        self.delta_text: list[str] = []
        self.calls: list[dict[str, Any]] = []
        self.model: str | None = None


def reconstruct_run(trace: Any) -> ObsRunSchema:  # noqa: PLR0912, PLR0915
    """Turn a captured RunTrace into spans + events + usage."""
    # Imported here: the routers package imports graph_service, which imports this module.
    from tenxgraph_api.src.app.routers.graph.schemas.graph_schemas import (
        ObsEventSchema,
        ObsRunSchema,
        ObsSpanSchema,
        ObsTokenUsageSchema,
    )

    records = list(trace.records)
    started = trace.started_at
    first_ts = next((r.get("timestamp") for r in records if r.get("timestamp")), started)
    base = started or first_ts or 0.0
    last_ts = max([r.get("timestamp") or base for r in records] + [trace.finished_at or base])
    total_ms = max(0.0, (last_ts - base) * MS_PER_SECOND)

    def off_ms(ts: float | None) -> float:
        return max(0.0, ((ts or base) - base) * MS_PER_SECOND)

    usage = ObsTokenUsageSchema()
    events: list[ObsEventSchema] = []
    root = ObsSpanSchema(
        id="root",
        name="graph",
        kind="root",
        parent=None,
        start_ms=0.0,
        duration_ms=total_ms,
        status=_STATUS_MAP.get(trace.status, "ok"),
    )
    spans: list[ObsSpanSchema] = [root]
    visits: list[_Visit] = []
    visit_counts: dict[str, int] = {}
    cur: _Visit | None = None
    tool_spans: dict[str, ObsSpanSchema] = {}
    pending_calls: dict[str, dict[str, Any]] = {}
    seen_llm_ids: set[str] = set()
    models: list[str] = []
    run_error: str | None = None
    first_user: str | None = None
    last_assistant: str | None = None
    last_message: dict[str, Any] | None = None
    span_seq = 0

    def new_span_id() -> str:
        nonlocal span_seq
        span_seq += 1
        return f"s{span_seq}"

    def parent_id() -> str:
        return cur.span.id if cur else "root"

    for idx, rec in enumerate(records):
        node = rec.get("node") or ""
        ts = rec.get("timestamp") or base
        step = rec.get("step")

        # A new node visit starts when the node changes, or the step counter moves on.
        if node and (
            cur is None
            or node != cur.node
            or (step is not None and cur.step is not None and step != cur.step)
        ):
            visit_counts[node] = visit_counts.get(node, 0) + 1
            span = ObsSpanSchema(
                id=new_span_id(),
                name=f"node: {node}",
                kind="node",
                parent="root",
                start_ms=off_ms(ts),
                duration_ms=0.0,
                step=step,
                visit=visit_counts[node],
                inputs={"incoming_message": last_message} if last_message else None,
            )
            cur = _Visit(span, node, step, ts)
            visits.append(cur)
            spans.append(span)
        if cur is not None and node:
            if cur.step is None and step is not None:
                cur.span.step = cur.step = step
            cur.last_ts = max(cur.last_ts, ts)
            cur.span.duration_ms = max(0.0, off_ms(cur.last_ts) - cur.span.start_ms)

        status = rec.get("status")
        call_id = rec.get("tool_call_id")

        if rec.get("error") or rec.get("is_error"):
            err = rec.get("error") or "error"
            run_error = run_error or err
            target = tool_spans.get(call_id) if call_id else None
            if target is not None:
                target.status = "error"
                target.error = err
                target.duration_ms = max(0.0, off_ms(ts) - target.start_ms)
            elif cur is not None:
                cur.error = cur.error or err

        if rec.get("model"):
            if rec["model"] not in models:
                models.append(rec["model"])
            if cur is not None:
                cur.model = rec["model"]

        # Tool execution starts (tool node announces it) -> tool span.
        if status == "invoking_tool" and call_id and call_id not in tool_spans:
            name = rec.get("tool_name") or "tool"
            call = pending_calls.get(call_id) or {}
            tspan = ObsSpanSchema(
                id=new_span_id(),
                name=f"tool: {name}",
                kind="tool",
                parent=parent_id(),
                start_ms=off_ms(ts),
                duration_ms=0.0,
                status="running" if trace.status == "running" else "ok",
                inputs={
                    "args": rec.get("args") if rec.get("args") is not None else call.get("args")
                },
                tool_name=name,
                tool_call_id=call_id,
            )
            tool_spans[call_id] = tspan
            spans.append(tspan)

        if rec.get("tool_calls"):
            for call in rec["tool_calls"]:
                cid = call.get("id")
                if cid and cid not in pending_calls:
                    pending_calls[cid] = {
                        "name": call.get("name"),
                        "args": call.get("args"),
                        "ts": ts,
                        "parent": parent_id(),
                    }
                if cur is not None and call not in cur.calls:
                    cur.calls.append(call)

        # Tool results close the tool span (create one for runs without tool updates).
        for res in rec.get("tool_results") or []:
            cid = res.get("call_id")
            result_span = tool_spans.get(cid) if cid else None
            if result_span is None and cid:
                call = pending_calls.get(cid) or {}
                name = call.get("name") or rec.get("tool_name") or "tool"
                start = call.get("ts", ts)
                result_span = ObsSpanSchema(
                    id=new_span_id(),
                    name=f"tool: {name}",
                    kind="tool",
                    parent=call.get("parent") or parent_id(),
                    start_ms=off_ms(start),
                    duration_ms=0.0,
                    inputs={"args": call.get("args")} if call else None,
                    tool_name=name,
                    tool_call_id=cid,
                )
                tool_spans[cid] = result_span
                spans.append(result_span)
            if result_span is not None and result_span.outputs is None:
                result_span.duration_ms = max(0.0, off_ms(ts) - result_span.start_ms)
                result_span.outputs = {"result": res.get("output")}
                if res.get("is_error"):
                    result_span.status = "error"
                    result_span.error = result_span.error or clip(res.get("output")) or "tool error"
                else:
                    result_span.status = "ok"
            if cur is not None:
                cur.boundary = max(cur.boundary, ts)

        # Message bookkeeping (complete messages only).
        if rec.get("role") and not rec.get("is_delta"):
            mid = rec.get("message_id")
            summary = _summarize_message(rec)
            if rec.get("role") == "user" and first_user is None and rec.get("text"):
                first_user = rec["text"]
            if rec.get("role") == "assistant" and rec.get("text"):
                last_assistant = rec["text"]
            if cur is not None and (not mid or mid not in cur.seen_ids):
                cur.messages.append(summary)
                if mid:
                    cur.seen_ids.add(mid)
            last_message = summary
        elif rec.get("is_delta") and cur is not None and rec.get("text"):
            cur.delta_text.append(rec["text"])

        # LLM span: a message that carries token usage.
        u = rec.get("usages")
        mid = rec.get("message_id")
        if u and not (mid and mid in seen_llm_ids):
            if mid:
                seen_llm_ids.add(mid)
            prompt = int(u.get("prompt_tokens", 0) or 0)
            completion = int(u.get("completion_tokens", 0) or 0)
            total = int(u.get("total_tokens", 0) or prompt + completion)
            usage.prompt_tokens += prompt
            usage.completion_tokens += completion
            usage.reasoning_tokens += int(u.get("reasoning_tokens", 0) or 0)
            usage.total_tokens += total
            start_ts = min(cur.boundary, ts) if cur else base
            out: dict[str, Any] = {}
            text = rec.get("text") or ("".join(cur.delta_text) if cur and cur.delta_text else "")
            if text:
                out["text"] = text
            calls = rec.get("tool_calls") or (cur.calls if cur else [])
            if calls:
                out["tool_calls"] = list(calls)
            spans.append(
                ObsSpanSchema(
                    id=new_span_id(),
                    name="llm.generate",
                    kind="llm",
                    parent=parent_id(),
                    start_ms=off_ms(start_ts),
                    duration_ms=max(0.0, off_ms(ts) - off_ms(start_ts)),
                    outputs=out or None,
                    model=rec.get("model") or (cur.model if cur else None),
                    input_tokens=prompt,
                    output_tokens=completion,
                    total_tokens=total,
                    reasoning_tokens=int(u.get("reasoning_tokens", 0) or 0),
                )
            )
            if cur is not None:
                cur.boundary = max(cur.boundary, ts)
                cur.delta_text = []
                cur.calls = []

        events.append(
            ObsEventSchema(
                id=f"e{idx}",
                type=rec.get("event") or "message",
                node=node,
                offset_ms=off_ms(ts),
                summary=event_summary(rec),
            )
        )

    # Finish node spans: outputs, status.
    for i, visit in enumerate(visits):
        if visit.messages:
            visit.span.outputs = {"messages": visit.messages}
        if visit.error:
            visit.span.status = "error"
            visit.span.error = visit.error
        elif trace.status == "running" and i == len(visits) - 1:
            visit.span.status = "running"

    root.inputs = {"user_message": first_user} if first_user else None
    root.outputs = {"assistant_message": last_assistant} if last_assistant else None
    if trace.status == "error" or run_error:
        root.status = "error"
        root.error = run_error

    spans.sort(key=lambda s: s.start_ms)  # stable: root stays first
    events.reverse()

    notes = ["The LLM prompt is not recorded, so llm spans have no inputs."]
    if records and not any(r.get("node") for r in records):
        notes.append(
            "No per-node events were recorded (non-streaming run): spans hang off the root "
            "and timings come from message creation times."
        )
    else:
        notes.append("Node inputs show only the latest message seen before the visit.")
    if len(records) >= MAX_RECORDS:
        notes.append("Trace truncated: only the most recent 2000 chunks are kept.")

    return ObsRunSchema(
        run_id=trace.run_id,
        thread_id=trace.thread_id,
        status=trace.status,
        started_at=trace.started_at,
        finished_at=trace.finished_at,
        duration_ms=total_ms,
        spans=spans,
        events=events,
        usage=usage,
        llm_calls=sum(1 for s in spans if s.kind == "llm"),
        tool_calls=sum(1 for s in spans if s.kind == "tool"),
        iterations=len(visits),
        error=run_error,
        model=models[0] if models else None,
        models=models,
        notes=notes,
    )
