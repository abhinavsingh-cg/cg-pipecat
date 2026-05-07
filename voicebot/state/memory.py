"""
Conversation memory — protocol + in-memory impl for local dev.

RedisMemory is the production store (state/redis_memory.py). InMemoryMemory
is a drop-in for local WebRTC testing so you don't need a Redis server
running. Both implement the same interface; pipeline.build_and_run accepts
either.
"""
from __future__ import annotations

import time
from typing import Iterable, List, Protocol


class ConversationMemory(Protocol):
    call_id: str

    async def close(self) -> None: ...
    async def load(self) -> List[dict]: ...
    async def seed_if_empty(self, messages: Iterable[dict]) -> List[dict]: ...
    async def append(self, role: str, content: str) -> None: ...
    async def trim_last(self, n: int) -> int: ...


class InMemoryMemory:
    """Process-local conversation log — no persistence across restarts."""

    def __init__(self, call_id: str):
        self.call_id = call_id
        self._history: List[dict] = []

    async def close(self) -> None:
        return

    async def load(self) -> List[dict]:
        return list(self._history)

    async def seed_if_empty(self, messages: Iterable[dict]) -> List[dict]:
        if self._history:
            return list(self._history)
        self._history = [
            {"role": m["role"], "content": m["content"], "timestamp": time.time()}
            for m in messages
        ]
        return list(self._history)

    async def append(self, role: str, content: str) -> None:
        self._history.append({"role": role, "content": content, "timestamp": time.time()})

    async def trim_last(self, n: int) -> int:
        if not self._history:
            return 0
        drop = min(n, len(self._history))
        self._history = self._history[:-drop]
        return drop
