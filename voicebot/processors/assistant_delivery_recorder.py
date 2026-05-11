"""
Assistant delivery tracking.

Records assistant text based on audio that actually reached the output side of
the pipeline, with periodic partial updates while speech is still in flight.
"""
from __future__ import annotations

import logging

from pipecat.frames.frames import (
    BotStoppedSpeakingFrame,
    CancelFrame,
    EndFrame,
    Frame,
    InterruptionFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    OutputAudioRawFrame,
    TTSSpeakFrame,
    TextFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from voicebot.state.assistant_speech import AssistantSpeechTracker
from voicebot.state.memory import ConversationMemory

logger = logging.getLogger(__name__)


class AssistantTextCollector(FrameProcessor):
    """
    Tracks assistant source text before it reaches the TTS engine.
    """

    def __init__(self, tracker: AssistantSpeechTracker):
        super().__init__()
        self._tracker = tracker

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)

        if isinstance(frame, TTSSpeakFrame) and getattr(frame, "text", "").strip():
            self._tracker.start_tts_utterance(frame.text)
        elif direction == FrameDirection.DOWNSTREAM:
            if isinstance(frame, LLMFullResponseStartFrame):
                self._tracker.start_llm_response()
            elif isinstance(frame, TextFrame) and frame.text:
                self._tracker.add_text_chunk(frame.text)
            elif isinstance(frame, LLMFullResponseEndFrame):
                self._tracker.finish_llm_response()

        await self.push_frame(frame, direction)


class AssistantGeneratedAudioRecorder(FrameProcessor):
    """
    Counts synthesized assistant audio frames before the output transport.
    """

    def __init__(self, tracker: AssistantSpeechTracker):
        super().__init__()
        self._tracker = tracker

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if direction == FrameDirection.DOWNSTREAM and isinstance(frame, OutputAudioRawFrame):
            self._tracker.note_generated_audio_frame()
        await self.push_frame(frame, direction)


class AssistantDeliveryRecorder(FrameProcessor):
    """
    Persists only the portion of assistant text that has actually been spoken.
    """

    def __init__(self, memory: ConversationMemory, tracker: AssistantSpeechTracker):
        super().__init__()
        self._memory = memory
        self._tracker = tracker
        self._assistant_turn_open = False

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)

        if direction == FrameDirection.DOWNSTREAM and isinstance(frame, OutputAudioRawFrame):
            await self._persist_if_needed(self._tracker.note_delivered_audio_frame())
        elif isinstance(frame, BotStoppedSpeakingFrame):
            await self._flush_current_utterance()
        elif isinstance(frame, (InterruptionFrame, CancelFrame, EndFrame)):
            await self._flush_current_utterance()

        await self.push_frame(frame, direction)

    async def _flush_current_utterance(self) -> None:
        await self._persist_if_needed(self._tracker.flush_current())
        self._tracker.finish_current_utterance()
        self._assistant_turn_open = False

    async def _persist_if_needed(self, text: str | None) -> None:
        normalized = (text or "").strip()
        if not normalized:
            return
        try:
            if self._assistant_turn_open:
                await self._memory.update_last("assistant", normalized)
            else:
                await self._memory.append("assistant", normalized)
                self._assistant_turn_open = True
        except Exception as exc:
            logger.warning("assistant delivery persist failed: %s", exc)
