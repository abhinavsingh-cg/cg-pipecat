"""
LanguageSuffixProcessor — two jobs in one processor:

  1. LANGUAGE DETECTION & SWITCHING (always active):
     On each TranscriptionFrame, tries to detect the language (using langdetect
     as a text-level fallback if SpeechBrain LIDProcessor hasn't already set
     state.candidate_language). After LANG_SWITCH_THRESHOLD consecutive turns
     in the new language, commits the switch and calls on_language_switch() so
     the TTS service is updated to speak in the new language.

  2. LANGUAGE SUFFIX INJECTION (only when inject_suffix=True):
     Appends a per-language instruction to the user's transcript, e.g.:
       "कैसे हो : इसका जवाब हिंदी भाषा में दे"
     This is only needed for the Credgenics HTTP STT, which doesn't embed
     language metadata. Sarvam/Deepgram carry their own signal so the suffix
     is left off to avoid polluting the LLM context.

CUSTOMIZE:
  • Change detection sensitivity: edit LANG_SWITCH_THRESHOLD in config.py.
  • Change suffix text: edit SUPPORTED_LNG_SUFFIX in config.py.
  • Add a language: add entries to SUPPORTED_LNG_SUFFIX, SPEECHBRAIN_LABEL_MAP,
    LANGDETECT_MAP in config.py, and to ARE_YOU_THERE_TEXT in are_you_there.py.
  • To completely disable language switching: return early from _handle() or
    remove this processor from the pipeline.
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

        # Short utterances (≤3 words) are unreliable for language detection —
        # keep the current language and skip LID entirely.
        if len(frame.text.split()) <= 3:
            if self._inject_suffix:
                suffix = SUPPORTED_LNG_SUFFIX.get(s.current_language, "")
                if suffix:
                    frame.text = f"{frame.text}{suffix}"
            return

        # Text-level LID fallback: only fires if SpeechBrain LIDProcessor
        # didn't already propose a candidate (short utterance, no audio LID).
        if not s.candidate_language:
            fallback = detect_language_from_text(frame.text)
            if fallback and fallback in s.supported_languages:
                update_candidate(s, fallback)

        # Commit the language switch once hysteresis threshold is reached.
        if s.candidate_language and s.candidate_count >= LANG_SWITCH_THRESHOLD:
            old = s.current_language
            new = commit_switch(s)
            logger.info("language switch: %s -> %s at turn %d", old, new, s.turn_count)
            if self._on_switch is not None:
                try:
                    self._on_switch(old, new)
                except Exception as exc:
                    logger.warning("on_language_switch raised: %s", exc)

        # Append the language-direction suffix (Credgenics STT only).
        if self._inject_suffix:
            suffix = SUPPORTED_LNG_SUFFIX.get(s.current_language, "")
            if suffix:
                frame.text = f"{frame.text}{suffix}"
