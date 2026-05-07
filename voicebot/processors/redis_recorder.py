"""
Redis recorders — persist user and assistant turns into Redis as the pipeline
runs.

Two FrameProcessors:
  - RedisUserRecorder: appends on each final TranscriptionFrame.
  - RedisAssistantRecorder: accumulates LLM TextFrames between
    LLMFullResponseStartFrame and LLMFullResponseEndFrame, appends on end.
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
