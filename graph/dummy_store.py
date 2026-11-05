"""
Dummy in-memory BaseStore — no embeddings, no external DB, no API key.

Purpose: let the playground's Memory inspector exercise the full store API
(list / search / get / create / update / delete / forget) against real HTTP
endpoints with deterministic, seeded data. Search is a naive case-insensitive
substring match with a fake similarity score — enough to see the UI work.

Referenced in 10xgraph.json as ``"store": "graph.dummy_store:store"``. Swap for a
real ``QdrantStore``/``PgVectorStore`` when embeddings + a vector DB are available.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from tenxgraph.core.state import Message
from tenxgraph.storage.store import BaseStore
from tenxgraph.storage.store.store_schema import MemorySearchResult, MemoryType


def _now() -> datetime:
    return datetime.now(UTC)


def _content_str(content: str | Message) -> str:
    if isinstance(content, Message):
        parts = []
        for block in content.content or []:
            text = getattr(block, "text", None)
            if text:
                parts.append(text)
        return " ".join(parts) or str(content)
    return str(content)


class DummyInMemoryStore(BaseStore):
    """A tiny dict-backed store implementing the BaseStore async surface."""

    def __init__(self) -> None:
        # memory_id -> MemorySearchResult
        self._mem: dict[str, MemorySearchResult] = {}
        self._seed()

    # ---- seed data --------------------------------------------------------- #

    def _seed(self) -> None:
        base = _now()
        samples = [
            (
                "User prefers metric units and 24-hour time.",
                MemoryType.SEMANTIC,
                "preferences",
                {"source": "conversation", "confidence": 0.94},
            ),
            (
                "User lives in Dhaka, Bangladesh.",
                MemoryType.ENTITY,
                "profile",
                {"entity": "location", "confidence": 0.99},
            ),
            (
                "On 2026-06-30 the user asked for the Q2 growth report and had it emailed.",
                MemoryType.EPISODIC,
                "history",
                {"thread_id": "th_q2report", "tools": ["get_report", "send_email"]},
            ),
            (
                "To summarise a report: fetch it, extract 3 metrics, draft 2 sentences.",
                MemoryType.PROCEDURAL,
                "skills",
                {"steps": 3},
            ),
            (
                "User's manager is Amina; escalate blockers to her.",
                MemoryType.RELATIONSHIP,
                "profile",
                {"entity": "person", "relation": "manager"},
            ),
            (
                "User dislikes long preambles — answer directly first, explain after.",
                MemoryType.SEMANTIC,
                "preferences",
                {"source": "feedback", "confidence": 0.88},
            ),
        ]
        for i, (content, mtype, category, meta) in enumerate(samples):
            mid = f"mem_seed_{i:02d}"
            self._mem[mid] = MemorySearchResult(
                id=mid,
                content=content,
                score=0.0,
                memory_type=mtype,
                metadata={"category": category, **meta},
                user_id="anonymous",
                thread_id=meta.get("thread_id"),
                timestamp=base - timedelta(hours=i * 5),
            )

    # ---- lifecycle --------------------------------------------------------- #

    async def asetup(self) -> Any:
        return None

    async def arelease(self) -> None:
        return None

    # ---- writes ------------------------------------------------------------ #

    async def astore(
        self,
        config: dict[str, Any],
        content: str | Message,
        memory_type: MemoryType = MemoryType.EPISODIC,
        category: str = "general",
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> str:
        mid = f"mem_{uuid.uuid4().hex[:12]}"
        self._mem[mid] = MemorySearchResult(
            id=mid,
            content=_content_str(content),
            score=0.0,
            memory_type=memory_type,
            metadata={"category": category, **(metadata or {})},
            user_id=config.get("user_id", "anonymous"),
            thread_id=config.get("thread_id"),
            timestamp=_now(),
        )
        return mid

    async def aupdate(
        self,
        config: dict[str, Any],
        memory_id: str,
        content: str | Message,
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> Any:
        existing = self._mem.get(memory_id)
        if not existing:
            return None
        existing.content = _content_str(content)
        if metadata:
            existing.metadata = {**existing.metadata, **metadata}
        existing.timestamp = _now()
        return memory_id

    async def adelete(self, config: dict[str, Any], memory_id: str, **kwargs: Any) -> Any:
        self._mem.pop(memory_id, None)
        return None

    async def aforget_memory(self, config: dict[str, Any], **kwargs: Any) -> Any:
        # Forget everything for the requesting user.
        uid = config.get("user_id", "anonymous")
        removed = [k for k, v in self._mem.items() if v.user_id in (uid, "anonymous")]
        for k in removed:
            self._mem.pop(k, None)
        return {"forgotten": len(removed)}

    # ---- reads ------------------------------------------------------------- #

    async def aget(
        self, config: dict[str, Any], memory_id: str, **kwargs: Any
    ) -> MemorySearchResult | None:
        return self._mem.get(memory_id)

    async def aget_all(
        self, config: dict[str, Any], limit: int = 100, **kwargs: Any
    ) -> list[MemorySearchResult]:
        rows = sorted(
            self._mem.values(),
            key=lambda m: m.timestamp or _now(),
            reverse=True,
        )
        return rows[:limit]

    async def asearch(
        self,
        config: dict[str, Any],
        query: str,
        memory_type: MemoryType | None = None,
        category: str | None = None,
        limit: int = 10,
        **kwargs: Any,
    ) -> list[MemorySearchResult]:
        q = (query or "").lower().strip()
        hits: list[MemorySearchResult] = []
        for m in self._mem.values():
            if memory_type and m.memory_type != memory_type:
                continue
            if category and m.metadata.get("category") != category:
                continue
            # Naive relevance: substring match -> high score, else token overlap.
            content_l = m.content.lower()
            if not q:
                score = 0.5
            elif q in content_l:
                score = 0.95
            else:
                terms = set(q.split())
                overlap = len(terms & set(content_l.split()))
                if overlap == 0:
                    continue
                score = min(0.9, 0.4 + 0.15 * overlap)
            hit = m.model_copy(update={"score": round(score, 3)})
            hits.append(hit)
        hits.sort(key=lambda m: m.score, reverse=True)
        return hits[:limit]


# The instance 10xgraph.json points at.
store = DummyInMemoryStore()
