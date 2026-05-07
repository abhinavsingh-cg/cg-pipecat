"""
EventLogger — logs interruption / speaking / LLM lifecycle frames.

Drop in to verify VAD-driven barge-in is firing, the bot speaking
windows are detected, and LLM responses are flowing.
"""
from __future__ import annotations

import logging

from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    Frame,
    InterruptionFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
    TTSStartedFrame,
    TTSStoppedFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

logger = logging.getLogger("voicebot.events")


class EventLogger(FrameProcessor):
    def __init__(self, label: str = "events"):
        super().__init__()
        self._label = label
        self._llm_buf: list[str] = []

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        name = type(frame).__name__

        if isinstance(frame, InterruptionFrame):
            logger.info("[%s] INTERRUPTION (%s)", self._label, direction.name)
        elif isinstance(frame, (UserStartedSpeakingFrame, VADUserStartedSpeakingFrame)):
            logger.info("[%s] user-started-speaking (%s)", self._label, name)
        elif isinstance(frame, (UserStoppedSpeakingFrame, VADUserStoppedSpeakingFrame)):
            logger.info("[%s] user-stopped-speaking (%s)", self._label, name)
        elif isinstance(frame, BotStartedSpeakingFrame):
            logger.info("[%s] bot-started-speaking", self._label)
        elif isinstance(frame, BotStoppedSpeakingFrame):
            logger.info("[%s] bot-stopped-speaking", self._label)
        elif isinstance(frame, TTSStartedFrame):
            logger.info("[%s] tts-started", self._label)
        elif isinstance(frame, TTSStoppedFrame):
            logger.info("[%s] tts-stopped", self._label)
        elif isinstance(frame, LLMFullResponseStartFrame):
            self._llm_buf = []
            logger.info("[%s] llm-response-start", self._label)
        elif isinstance(frame, LLMTextFrame):
            self._llm_buf.append(frame.text)
        elif isinstance(frame, LLMFullResponseEndFrame):
            text = "".join(self._llm_buf).strip()
            self._llm_buf = []
            logger.info("[%s] llm-response-end text=%r", self._label, text)

        await self.push_frame(frame, direction)
