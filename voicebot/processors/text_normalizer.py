"""
TextNormalizationProcessor — replaces numeric digits in transcripts with words.

Sits after STT in the pipeline. Converts "call me at 12345" to
"call me at twelve thousand three hundred and forty-five" so the LLM
sees natural language rather than bare digits.

CUSTOMIZE:
  • Change word language: pass lang='hi' for Hindi words.
  • Disable: remove from the procs list in pipeline.py.
"""
from __future__ import annotations

import logging
import re
from typing import Optional
from voicebot.config import LANGUAGE_TO_CODES

from pipecat.frames.frames import (
    Frame,
    InterruptionFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    TextFrame,
    TranscriptionFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from voicebot.state.language_state import LanguageState

logger = logging.getLogger(__name__)

try:
    from num2words import num2words as _num2words
    _HAS_NUM2WORDS = True
except ImportError:
    _HAS_NUM2WORDS = False
    logger.warning("num2words not installed — TextNormalizationProcessor is a no-op; pip install num2words")


def _replace_numbers(text: str, lang: str = "en") -> str:
    if not _HAS_NUM2WORDS:
        return text

    def _sub(m: re.Match) -> str:
        try:
            return _num2words(int(m.group()), lang=lang)
        except Exception:
            return m.group()

    return re.sub(r"\b\d+\b", _sub, text)


class TextNormalizationProcessor(FrameProcessor):
    """
    Mutates TranscriptionFrame.text in-place: replaces digit sequences with
    their spoken-word equivalent before the frame reaches language detection
    or the LLM context aggregator.

    Args:
        lang:  num2words language code ("en", "hi", "te", etc.).
        state: shared LanguageState — used only for logging current_language.
    """

    def __init__(self, lang: str = "en", state: Optional[LanguageState] = None):
        super().__init__()
        self._lang = lang
        self._state = state
        # Buffer holds a trailing digit run that may continue in the next chunk.
        # Ported from rtp_processor.py:1645-1662 — defer flushing while the tail
        # is mid-number so "202" + "6" doesn't normalize to "two hundred and two
        # six" instead of "two thousand twenty-six".
        self._pending: str = ""

    def _resolve_num2words_lang(self) -> Optional[str]:
        if self._state is None:
            return self._lang
        codes = LANGUAGE_TO_CODES.get(self._state.current_language)
        return codes if codes else self._lang

    async def _flush_pending(self, direction: FrameDirection) -> None:
        if not self._pending:
            return
        normalized = _replace_numbers(self._pending, self._resolve_num2words_lang())
        if normalized != self._pending:
            logger.info(
                "llm_text_normalized | raw=%r | normalized=%r",
                self._pending,
                normalized,
            )
        await self.push_frame(TextFrame(text=normalized), direction)
        self._pending = ""

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)

        # STT-side log (only fires if this processor is moved upstream of
        # context_aggr.user(); see note in pipeline.py).
        if isinstance(frame, TranscriptionFrame) and frame.text:
            current_lang = self._state.current_language if self._state else "unknown"
            logger.info(
                "stt_transcript | text=%r | stt_lang=%s | current_lang=%s",
                frame.text,
                frame.language or "n/a",
                current_lang,
            )
            await self.push_frame(frame, direction)
            return

        # New LLM response — drop any stale pending tail from prior turn.
        if isinstance(frame, LLMFullResponseStartFrame):
            self._pending = ""
            await self.push_frame(frame, direction)
            return

        # End of LLM response — flush whatever digits we were holding.
        if isinstance(frame, LLMFullResponseEndFrame):
            await self._flush_pending(direction)
            await self.push_frame(frame, direction)
            return

        # Bot was interrupted — drop in-flight pending so it doesn't bleed
        # into the next response.
        if isinstance(frame, InterruptionFrame):
            self._pending = ""
            await self.push_frame(frame, direction)
            return

        # Streaming LLM token — coalesce trailing digit runs across chunks,
        # then normalize the safe prefix and emit.
        if isinstance(frame, TextFrame) and frame.text:
            current_lang = self._state.current_language if self._state else "unknown"
            logger.info("llm_text | text=%r | current_lang=%s", frame.text, current_lang)

            combined = self._pending + frame.text
            i = len(combined)
            while i > 0 and combined[i - 1].isdigit():
                i -= 1
            emit, self._pending = combined[:i], combined[i:]

            if not emit:
                # Whole chunk is part of an unfinished number — swallow this
                # frame; we'll emit when a non-digit char arrives.
                return

            normalized = _replace_numbers(emit, self._resolve_num2words_lang())
            if normalized != emit:
                logger.info("llm_text_normalized | raw=%r | normalized=%r", emit, normalized)
            frame.text = normalized
            await self.push_frame(frame, direction)
            return

        await self.push_frame(frame, direction)
