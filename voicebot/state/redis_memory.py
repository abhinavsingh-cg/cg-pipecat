"""
Redis-backed conversation memory.

Schema preserved from credgenics-voice-bot-library:
  Key:   call:{call_id}:data
  Value: JSON list of {"role", "content", "timestamp"} dicts
  TTL:   CONTEXT_EXPIRY_SECONDS (24 h)

Initial seeding (BASIC_BLOCK + system prompt + initial user prompt) mirrors
rtp_processor.py:2539-2546.
"""
from __future__ import annotations

import json
import logging
from typing import Iterable, List, Optional

import redis.asyncio as aioredis

from voicebot.config import CONTEXT_EXPIRY_SECONDS, REDIS_URL
from voicebot.state.memory import (
    DEFAULT_STAGE,
    ConversationCallStore,
    conversation_entry,
)

logger = logging.getLogger(__name__)


def _key(call_id: str) -> str:
    return f"call:{call_id}:data"


class RedisMemory:
    """
    Append-only conversation log with trim helpers + node/stage tracking.

    Storage shape (current): {"history": [...], "stage": "<stage_name>"}.
    Back-compat: legacy records were a bare list of message dicts; they're
    loaded as `history` with `stage` defaulted to DEFAULT_STAGE.
    """

    def __init__(
        self,
        call_id: str,
        client: Optional[aioredis.Redis] = None,
        conversation_store: Optional[ConversationCallStore] = None,
    ):
        self.call_id = call_id
        self._client = client or aioredis.from_url(REDIS_URL, decode_responses=True)
        self._conversation_store = (
            conversation_store or ConversationCallStore(redis_client=self._client)
        )

    async def close(self) -> None:
        await self._conversation_store.close()
        await self._client.aclose()

    async def _read_raw(self) -> tuple[List[dict], str]:
        raw = await self._client.get(_key(self.call_id))
        if not raw:
            return [], DEFAULT_STAGE
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("redis_memory: corrupt history for %s — discarding", self.call_id)
            return [], DEFAULT_STAGE
        if isinstance(parsed, list):
            # Legacy shape — bare list of turns.
            return parsed, DEFAULT_STAGE
        if isinstance(parsed, dict):
            history = parsed.get("history") or []
            stage = parsed.get("stage") or DEFAULT_STAGE
            return history, stage
        return [], DEFAULT_STAGE

    async def load(self) -> List[dict]:
        history, _ = await self._read_raw()
        return history

    async def seed_if_empty(self, messages: Iterable[dict]) -> List[dict]:
        existing, _ = await self._read_raw()
        if existing:
            return existing
        seeded = [
            conversation_entry(m["role"], m["content"])
            for m in messages
        ]
        await self._save(seeded, DEFAULT_STAGE)
        return seeded

    async def append(self, role: str, content: str) -> None:
        history, stage = await self._read_raw()
        history.append(conversation_entry(role, content))
        await self._save(history, stage)
        await self._conversation_store.publish(self.call_id, history)

    async def trim_last(self, n: int) -> int:
        """Drop the last N messages (used by the early-barge-in concat hook)."""
        history, stage = await self._read_raw()
        if not history:
            return 0
        drop = min(n, len(history))
        updated = history[:-drop]
        await self._save(updated, stage)
        await self._conversation_store.publish(self.call_id, updated)
        return drop

    async def get_stage(self) -> str:
        _, stage = await self._read_raw()
        return stage

    async def set_stage(self, stage: str) -> None:
        history, _ = await self._read_raw()
        await self._save(history, stage)

    async def _save(self, history: List[dict], stage: str) -> None:
        payload = json.dumps(
            {"history": history, "stage": stage}, ensure_ascii=False
        )
        async with self._client.pipeline(transaction=True) as pipe:
            pipe.set(_key(self.call_id), payload)
            pipe.expire(_key(self.call_id), CONTEXT_EXPIRY_SECONDS)
            await pipe.execute()
