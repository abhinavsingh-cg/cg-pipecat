"""
LanguageSuffixProcessor — commits hysteresis-gated language switches and
appends the per-language suffix to the user's transcript before it reaches
the LLM.

Ports /Users/admin/livekit/agent.py:on_user_turn_completed (lines 139-196)
to a Pipecat FrameProcessor.
"""
from __future__ import annotations

import logging
from typing import Callable, Optional

from pipecat.frames.frames import Frame, TranscriptionFrame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from voicebot.config import LANG_SWITCH_THRESHOLD, LANG_TO_ISO, SUPPORTED_LNG_SUFFIX
from voicebot.state.language_state import (
    LanguageState,
    commit_switch,
    detect_language_from_text,
    update_candidate,
)

logger = logging.getLogger(__name__)


class LanguageSuffixProcessor(FrameProcessor):
    """
    On each final TranscriptionFrame:
      1. Text-level LID fallback if SpeechBrain hasn't fired (short utterance).
      2. Commit hysteresis-gated switch if threshold reached.
      3. Notify TTS via on_language_switch callback (e.g. tts.set_language).
      4. Append SUPPORTED_LNG_SUFFIX[current_language] to frame.text — only
         when `inject_suffix=True`. External streaming STTs (Sarvam,
         Deepgram) already attach language metadata to the transcript and
         the LLM picks it up from the content + system prompt; the suffix
         hint is mainly useful for the in-house Credgenics HTTP STT, which
         is non-streaming and lacks that signal. Hysteresis + TTS update
         still happen regardless.
    """

    def __init__(
        self,
        state: LanguageState,
        on_language_switch: Optional[Callable[[str, str], None]] = None,
        *,
        inject_suffix: bool = False,
    ):
        super().__init__()
        self._state = state
        self._on_switch = on_language_switch
        self._inject_suffix = inject_suffix

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if isinstance(frame, TranscriptionFrame) and frame.text:
            self._handle(frame)
        await self.push_frame(frame, direction)

    def _handle(self, frame: TranscriptionFrame) -> None:
        s = self._state
        s.turn_count += 1

        # Text-level fallback only if SpeechBrain hasn't already proposed one.
        if not s.candidate_language:
            fallback = detect_language_from_text(frame.text)
            if fallback and fallback in s.supported_languages:
                update_candidate(s, fallback)

        if s.candidate_language and s.candidate_count >= LANG_SWITCH_THRESHOLD:
            old = s.current_language
            new = commit_switch(s)
            logger.info("language switch: %s -> %s at turn %d", old, new, s.turn_count)
            if self._on_switch is not None:
                try:
                    self._on_switch(old, new)
                except Exception as exc:
                    logger.warning("on_language_switch raised: %s", exc)

        if self._inject_suffix:
            suffix = SUPPORTED_LNG_SUFFIX.get(s.current_language, "")
            if suffix:
                frame.text = f"{frame.text}{suffix}"
