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


# Matches a number with optional thousands separators and an optional decimal:
#   12          → 12
#   10,000      → 10000  (commas stripped)
#   1,23,456    → 123456 (Indian grouping works too)
#   12.75       → 12.75
#   2,026.50    → 2026.50
_NUMBER_RE = re.compile(r"\d+(?:,\d+)*(?:\.\d+)?")

# End-of-call marker — when the LLM emits this token in its response,
# strip it before TTS/Redis see it and arm state.end_after_speech so the
# post-TTS trigger can hang up once the goodbye finishes playing.
END_MARKER = "| END |"


def _normalize_match(s: str, lang: str) -> str:
    """
    Spoken-form rendering for a single number match.
      • integer part   → num2words   ("ten thousand")
      • decimal part   → digit-by-digit, space-separated, prefixed with " point "
        e.g. 12.75 → "twelve point seven five"
    """
    if "." in s:
        int_part, dec_part = s.split(".", 1)
    else:
        int_part, dec_part = s, ""
    int_part = int_part.replace(",", "")
    try:
        int_words = _num2words(int(int_part), lang=lang)
    except Exception:
        return s
    if not dec_part:
        return int_words
    try:
        dec_words = " ".join(_num2words(int(d), lang=lang) for d in dec_part)
    except Exception:
        return s
    return f"{int_words} point {dec_words}"


def _replace_numbers(text: str, lang: str = "en") -> str:
    if not _HAS_NUM2WORDS:
        return text
    return _NUMBER_RE.sub(lambda m: _normalize_match(m.group(), lang), text)


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
        # codes = LANGUAGE_TO_CODES.get(self._state.current_language)
        codes = self._state.current_language
        return codes if codes else self._lang

    async def _flush_pending(self, direction: FrameDirection) -> None:
        if not self._pending:
            return
        # Strip any END_MARKER that survived in the tail buffer (e.g. the
        # response ended with "... | END |" and the prefix-tail logic was
        # holding it back waiting for more chunks).
        while True:
            idx = self._pending.find(END_MARKER)
            if idx < 0:
                break
            self._pending = self._pending[:idx] + self._pending[idx + len(END_MARKER):]
            if self._state is not None:
                self._state.end_after_speech = True
            logger.info("end_marker detected in flush; will hang up after bot stops speaking")
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
        # into the next response. Also disarm any pending end-of-call: an
        # interrupted goodbye should not trigger hangup.
        if isinstance(frame, InterruptionFrame):
            self._pending = ""
            if self._state is not None:
                self._state.end_after_speech = False
            await self.push_frame(frame, direction)
            return

        # Streaming LLM token — coalesce trailing digit runs across chunks,
        # then normalize the safe prefix and emit.
        if isinstance(frame, TextFrame) and frame.text:
            current_lang = self._state.current_language if self._state else "unknown"
            # logger.info("llm_text | text=%r | current_lang=%s", frame.text, current_lang)

            combined = self._pending + frame.text

            # Strip any complete END_MARKER occurrences and arm the
            # post-speech hangup flag. Done before the digit-tail split so
            # the marker can never appear in `emit` and reach TTS / Redis.
            while True:
                idx = combined.find(END_MARKER)
                if idx < 0:
                    break
                combined = combined[:idx] + combined[idx + len(END_MARKER):]
                if self._state is not None:
                    self._state.end_after_speech = True
                logger.info("end_marker detected; will hang up after bot stops speaking")

            # Walk back from the tail while we're "potentially mid-number".
            # A char is part of an ambiguous tail if it is a digit, OR it is
            # ',' / '.' AND the char before it is a digit (i.e. could be a
            # thousands separator or decimal point still being streamed).
            # This holds chunks like "10","," ,"000" together (→ "10,000")
            # and "12",".","75" together (→ "12.75").
            i = len(combined)
            while i > 0:
                ch = combined[i - 1]
                if ch.isdigit():
                    i -= 1
                elif ch in (",", ".") and i >= 2 and combined[i - 2].isdigit():
                    i -= 1
                else:
                    break

            # Also hold back any trailing run that could be the start of a
            # split END_MARKER (e.g. "...goodbye | EN" + "D |").
            marker_keep = 0
            max_k = min(len(END_MARKER) - 1, len(combined))
            for k in range(max_k, 0, -1):
                if combined[-k:] == END_MARKER[:k]:
                    marker_keep = k
                    break
            j = len(combined) - marker_keep
            boundary = min(i, j)
            emit, self._pending = combined[:boundary], combined[boundary:]

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
