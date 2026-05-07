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

from pipecat.frames.frames import Frame, TranscriptionFrame
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

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if isinstance(frame, TranscriptionFrame) and frame.text:
            raw = frame.text
            normalized = _replace_numbers(raw, self._lang)
            current_lang = self._state.current_language if self._state else "unknown"
            logger.info(
                "stt | raw=%r | stt_lang=%s | current_lang=%s | normalized=%r",
                raw,
                frame.language or "n/a",
                current_lang,
                normalized,
            )
            frame.text = normalized
        await self.push_frame(frame, direction)
