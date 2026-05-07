"""
Redis recorders — persist conversation turns as the pipeline runs.

Two FrameProcessors that write to ConversationMemory (Redis in production):

  RedisUserRecorder:
    Listens for TranscriptionFrame. Appends the transcript as a "user" message.
    Placed AFTER LanguageSuffixProcessor so the stored text includes the
    suffix (if inject_suffix=True), matching what the LLM received.

  RedisAssistantRecorder:
    Listens for LLMFullResponseStartFrame / TextFrame / LLMFullResponseEndFrame.
    Accumulates the full streamed response into a buffer, then appends it as
    an "assistant" message on LLMFullResponseEndFrame.
    Placed BEFORE tts so the assistant text is recorded regardless of whether
    TTS succeeds.

CUSTOMIZE:
  • To record additional metadata (language, timestamp, call_id):
    extend RedisMemory.append() to accept kwargs and pass them here.
  • To record only delivered text (skip interrupted responses):
    move RedisAssistantRecorder to AFTER transport.output() and wire it to
    BotStoppedSpeakingFrame — but be aware EarlyBargeInConcatProcessor
    depends on having the turn already in Redis to trim it on barge-in.
"""
from __future__ import annotations

import logging

from pipecat.frames.frames import (
    Frame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    TextFrame,
    TranscriptionFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from voicebot.state.redis_memory import RedisMemory

logger = logging.getLogger(__name__)


class RedisUserRecorder(FrameProcessor):
    def __init__(self, memory: RedisMemory):
        super().__init__()
        self._memory = memory

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if isinstance(frame, TranscriptionFrame) and frame.text:
            try:
                await self._memory.append("user", frame.text)
            except Exception as exc:
                logger.warning("redis user append failed: %s", exc)
        await self.push_frame(frame, direction)


class RedisAssistantRecorder(FrameProcessor):
    def __init__(self, memory: RedisMemory):
        super().__init__()
        self._memory = memory
        self._buf: list = []
        self._collecting = False

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if isinstance(frame, LLMFullResponseStartFrame):
            # New LLM response starting — clear any leftover buffer.
            self._buf.clear()
            self._collecting = True
        elif isinstance(frame, TextFrame) and self._collecting:
            if frame.text:
                self._buf.append(frame.text)
        elif isinstance(frame, LLMFullResponseEndFrame):
            self._collecting = False
            text = "".join(self._buf).strip()
            self._buf.clear()
            if text:
                try:
                    await self._memory.append("assistant", text)
                except Exception as exc:
                    logger.warning("redis assistant append failed: %s", exc)
        await self.push_frame(frame, direction)
