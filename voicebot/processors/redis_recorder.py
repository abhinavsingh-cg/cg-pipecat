"""
Conversation recorders — persist conversation turns as the pipeline runs.

FrameProcessors that write to ConversationMemory:

  RedisUserRecorder:
    Listens for TranscriptionFrame. Appends the transcript as a "user" message.
    Placed AFTER LanguageSuffixProcessor so the stored text includes the
    suffix (if inject_suffix=True), matching what the LLM received.
"""
from __future__ import annotations

import logging

from pipecat.frames.frames import (
    Frame,
    TranscriptionFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from voicebot.state.memory import ConversationMemory

logger = logging.getLogger(__name__)


class RedisUserRecorder(FrameProcessor):
    def __init__(self, memory: ConversationMemory):
        super().__init__()
        self._memory = memory

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if isinstance(frame, TranscriptionFrame) and frame.text:
            logger.info("user_transcript | text=%r", frame.text)
            try:
                await self._memory.append("user", frame.text)
            except Exception as exc:
                logger.warning("redis user append failed: %s", exc)
        await self.push_frame(frame, direction)
