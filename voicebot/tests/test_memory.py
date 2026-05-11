from __future__ import annotations

import asyncio
import json
import logging
import sys

from voicebot.state.memory import (
    ConversationCallStore,
    InMemoryMemory,
    conversation_entry,
    conversation_key_view,
)


class _CaptureHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


class _FakePipeline:
    def __init__(self, client) -> None:
        self._client = client
        self._ops: list[tuple[str, str, str | int]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    def set(self, key: str, value: str) -> None:
        self._ops.append(("set", key, value))
        self._client.kv_store[key] = value

    def expire(self, key: str, ttl: int) -> None:
        self._ops.append(("expire", key, ttl))
        self._client.expiry[key] = ttl

    async def execute(self) -> None:
        return None


class _FakeRedisClient:
    def __init__(self) -> None:
        self.kv_store: dict[str, str] = {}
        self.expiry: dict[str, int] = {}
        self.closed = False

    def pipeline(self, transaction: bool = True) -> _FakePipeline:
        assert transaction is True
        return _FakePipeline(self)

    async def aclose(self) -> None:
        self.closed = True


def test_conversation_key_view_filters_system_and_normalizes_timestamps() -> None:
    history = [
        {"role": "system", "content": "seed", "timestamp": 1710000000.0},
        {"role": "assistant", "content": "hello", "timestamp": 1710000001.0},
        {"role": "user", "content": "hi", "timestamp": "2026-05-08T17:14:33.122100"},
    ]
    view = conversation_key_view(history)

    assert [turn["role"] for turn in view] == ["assistant", "user"]
    assert view[0]["content"] == "hello"
    assert "T" in view[0]["timestamp"]
    assert view[1]["timestamp"] == "2026-05-08T17:14:33.122100"


async def _run_memory_append_test() -> None:
    redis_client = _FakeRedisClient()
    store = ConversationCallStore(redis_client=redis_client)
    memory = InMemoryMemory("call-123", conversation_store=store)
    logger = logging.getLogger("voicebot.conversation")
    handler = _CaptureHandler()
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)

    try:
        await memory.seed_if_empty([{"role": "system", "content": "seed"}])
        await memory.append("assistant", "नमस्ते")
        await memory.append("user", "हाँ जी")

        history = await memory.load()
        assert history[0]["role"] == "system"
        assert history[1]["role"] == "assistant"
        assert history[2]["role"] == "user"
        assert "T" in history[1]["timestamp"]
        assert len(handler.records) == 2

        last_record = handler.records[-1]
        assert last_record.getMessage() == "conversation_call updated"
        assert last_record.call_id == "call-123"
        turns = last_record.conversation_call
        assert turns == [
            {
                "role": "assistant",
                "content": "नमस्ते",
                "timestamp": history[1]["timestamp"],
            },
            {
                "role": "user",
                "content": "हाँ जी",
                "timestamp": history[2]["timestamp"],
            },
        ]
        redis_key = "call:call-123:data"
        redis_value = redis_client.kv_store[redis_key]
        assert "नमस्ते" in redis_value
        assert "हाँ जी" in redis_value
        assert json.loads(redis_value) == turns
        assert redis_client.expiry[redis_key] > 0
    finally:
        logger.removeHandler(handler)
        await memory.close()


async def _run_update_last_test() -> None:
    redis_client = _FakeRedisClient()
    memory = InMemoryMemory(
        "call-456",
        conversation_store=ConversationCallStore(redis_client=redis_client),
    )
    try:
        await memory.seed_if_empty([{"role": "system", "content": "seed"}])
        await memory.append("assistant", "नमस्ते")
        await memory.update_last("assistant", "नमस्ते, मैं आपकी मदद कर रही हूँ")

        history = await memory.load()
        assert len(history) == 2
        assert history[-1]["role"] == "assistant"
        assert history[-1]["content"] == "नमस्ते, मैं आपकी मदद कर रही हूँ"

        redis_value = redis_client.kv_store["call:call-456:data"]
        assert "मैं आपकी मदद कर रही हूँ" in redis_value
    finally:
        await memory.close()


def main() -> int:
    try:
        assert "T" in conversation_entry("assistant", "hello")["timestamp"]
        test_conversation_key_view_filters_system_and_normalizes_timestamps()
        asyncio.run(_run_memory_append_test())
        asyncio.run(_run_update_last_test())
    except AssertionError as exc:
        print(f"FAIL: {exc}")
        return 1
    print("memory tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
