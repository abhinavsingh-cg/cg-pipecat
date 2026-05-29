"""
RepeatPromptOnFailure — localized recovery prompt when an LLM turn fails.
"""
from __future__ import annotations

import logging

from pipecat.frames.frames import (
    ErrorFrame,
    Frame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    TTSSpeakFrame,
    TextFrame,
    TranscriptionFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from voicebot.prompts.runtime_messages import (
    INVALID_LANGUAGE_DETECTION,
    REPEAT_MESSAGE,
    localized_runtime_message,
)
from voicebot.state.language_state import LanguageState

logger = logging.getLogger(__name__)


class RepeatPromptOnFailure(FrameProcessor):
    def __init__(self, state: LanguageState):
        super().__init__()
        self._state = state
        self._awaiting_llm = False
        self._llm_active = False
        self._llm_text_seen = False

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)

        if direction == FrameDirection.DOWNSTREAM:
            if isinstance(frame, TranscriptionFrame) and frame.text:
                self._awaiting_llm = True
            elif isinstance(frame, LLMFullResponseStartFrame):
                self._llm_active = True
                self._llm_text_seen = False
            elif isinstance(frame, TextFrame) and self._llm_active and frame.text:
                self._llm_text_seen = True
            elif isinstance(frame, LLMFullResponseEndFrame):
                if self._llm_active and not self._llm_text_seen:
                    await self._emit_repeat("empty LLM response")
                self._reset()
            elif isinstance(frame, ErrorFrame):
                error = getattr(frame, "error", "")
                if "STT_INVALID_LANGUAGE" in error:
                    self._state.invalid_language_count += 1
                    if self._state.invalid_language_count >= 2:
                        logger.warning(
                            "stt invalid language (strike %d); emitting unsupported-language prompt",
                            self._state.invalid_language_count,
                        )
                        await self._emit_invalid_language()
                    else:
                        logger.warning(
                            "stt invalid language (strike %d); emitting repeat prompt",
                            self._state.invalid_language_count,
                        )
                        await self._emit_repeat("invalid language (first offense)")
                    self._reset()
                    return
                if self._awaiting_llm or self._llm_active:
                    logger.warning(
                        "llm failure detected; emitting repeat prompt | error=%s",
                        getattr(frame, "error", "unknown"),
                    )
                    await self._emit_repeat(getattr(frame, "error", "LLM failure"))
                    self._reset()
                    return

        await self.push_frame(frame, direction)

    async def _emit_repeat(self, reason: str) -> None:
        language_key = self._state.last_detected_language or self._state.current_language
        text = localized_runtime_message(REPEAT_MESSAGE, language_key)
        logger.info("repeat_prompt | lang=%s | reason=%s | text=%r", language_key, reason, text)
        await self.push_frame(TTSSpeakFrame(text=text, append_to_context=True), FrameDirection.UPSTREAM)

    async def _emit_invalid_language(self) -> None:
        language_key = self._state.last_detected_language or self._state.current_language
        text = localized_runtime_message(INVALID_LANGUAGE_DETECTION, language_key)
        logger.info("invalid_language_prompt | lang=%s | text=%r", language_key, text)
        await self.push_frame(TTSSpeakFrame(text=text, append_to_context=True), FrameDirection.UPSTREAM)

    def _reset(self) -> None:
        self._awaiting_llm = False
        self._llm_active = False
        self._llm_text_seen = False
