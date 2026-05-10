"""
Conversation memory — protocol + in-memory impl for local dev.

RedisMemory is the production store (state/redis_memory.py). InMemoryMemory
is a drop-in for local WebRTC testing so you don't need a Redis server
running. Both implement the same interface; pipeline.build_and_run accepts
either.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any, Iterable, List, Protocol

from voicebot.config import CONTEXT_EXPIRY_SECONDS, REDIS_URL
from voicebot.prompts.call_data import CALL_DATA_REDIS_KEY

conversation_logger = logging.getLogger("voicebot.conversation")
_CONVERSATION_ROLES = {"user", "assistant"}


def _timestamp_now() -> str:
    return datetime.now().isoformat()


def conversation_entry(role: str, content: str) -> dict[str, str]:
    return {
        "role": role,
        "content": content,
        "timestamp": _timestamp_now(),
    }


def _normalize_timestamp(timestamp: Any) -> str:
    if isinstance(timestamp, (int, float)):
        return datetime.fromtimestamp(timestamp).isoformat()
    if isinstance(timestamp, datetime):
        return timestamp.isoformat()
    if timestamp:
        return str(timestamp)
    return _timestamp_now()


def conversation_key_view(history: Iterable[dict[str, Any]]) -> list[dict[str, str]]:
    turns: list[dict[str, str]] = []
    for item in history:
        role = item.get("role")
        if role not in _CONVERSATION_ROLES:
            continue
        turns.append({
            "role": str(role),
            "content": str(item.get("content", "")),
            "timestamp": _normalize_timestamp(item.get("timestamp")),
        })
    return turns


class ConversationCallStore:
    def __init__(self, redis_client: Any | None = None, write_to_redis: bool = True):
        self._client = redis_client
        self._owns_client = redis_client is None
        self._write_to_redis_enabled = write_to_redis
        self._redis_error_logged = False

    async def publish(self, call_id: str, history: Iterable[dict[str, Any]]) -> None:
        turns = conversation_key_view(history)
        if not turns:
            return

        conversation_logger.info(
            "conversation_call updated",
            extra={
                "call_id": call_id,
                "conversation_call": turns,
                "redis_key": _conversation_redis_key(call_id),
            },
        )
        await self._write_to_redis(call_id, turns)

    async def close(self) -> None:
        if not self._owns_client or self._client is None:
            return
        await self._client.aclose()
        self._client = None

    async def _write_to_redis(self, call_id: str, turns: list[dict[str, str]]) -> None:
        if not self._write_to_redis_enabled:
            return
        try:
            client = await self._get_client()
            if client is None:
                return
            payload = json.dumps(turns, ensure_ascii=False)
            redis_key = _conversation_redis_key(call_id)
            async with client.pipeline(transaction=True) as pipe:
                pipe.set(redis_key, payload)
                pipe.expire(redis_key, CONTEXT_EXPIRY_SECONDS)
                await pipe.execute()
        except Exception as exc:
            if not self._redis_error_logged:
                conversation_logger.warning(
                    "conversation_call redis mirror failed for %s: %s",
                    call_id,
                    exc,
                )
                self._redis_error_logged = True

    async def _get_client(self) -> Any | None:
        if self._client is not None:
            return self._client
        if not REDIS_URL:
            return None
        import redis.asyncio as aioredis

        self._client = aioredis.from_url(REDIS_URL, decode_responses=True)
        return self._client


def _conversation_redis_key(call_id: str) -> str:
    return CALL_DATA_REDIS_KEY.format(call_id=call_id)


class ConversationMemory(Protocol):
    call_id: str

    async def close(self) -> None: ...
    async def load(self) -> List[dict]: ...
    async def seed_if_empty(self, messages: Iterable[dict]) -> List[dict]: ...
    async def append(self, role: str, content: str) -> None: ...
    async def update_last(self, role: str, content: str) -> None: ...
    async def trim_last(self, n: int) -> int: ...


class InMemoryMemory:
    """Process-local conversation log — no persistence across restarts."""

    def __init__(
        self,
        call_id: str,
        conversation_store: ConversationCallStore | None = None,
    ):
        self.call_id = call_id
        self._history: List[dict] = []
        self._conversation_store = conversation_store or ConversationCallStore()

    async def close(self) -> None:
        await self._conversation_store.close()

    async def load(self) -> List[dict]:
        return list(self._history)

    async def seed_if_empty(self, messages: Iterable[dict]) -> List[dict]:
        if self._history:
            return list(self._history)
        self._history = [
            conversation_entry(m["role"], m["content"])
            for m in messages
        ]
        return list(self._history)

    async def append(self, role: str, content: str) -> None:
        self._history.append(conversation_entry(role, content))
        await self._conversation_store.publish(self.call_id, self._history)

    async def update_last(self, role: str, content: str) -> None:
        normalized = content.strip()
        if not normalized:
            return
        if self._history and self._history[-1].get("role") == role:
            self._history[-1]["content"] = normalized
        else:
            self._history.append(conversation_entry(role, normalized))
        await self._conversation_store.publish(self.call_id, self._history)

    async def trim_last(self, n: int) -> int:
        if not self._history:
            return 0
        drop = min(n, len(self._history))
        self._history = self._history[:-drop]
        await self._conversation_store.publish(self.call_id, self._history)
        return drop
