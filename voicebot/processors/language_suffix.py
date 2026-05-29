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
import string
from typing import Callable, Optional

from pipecat.frames.frames import Frame, TranscriptionFrame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.transcriptions.language import Language as PipecatLanguage

from voicebot.config import (
    LANG_SWITCH_THRESHOLD,
    LANG_TO_ISO,
    STT_PRIMARY,
    SUPPORTED_LNG_SUFFIX,
)
from voicebot.state.language_state import (
    LanguageState,
    commit_switch,
    detect_language_from_text,
    update_candidate,
)

logger = logging.getLogger(__name__)


def _is_single_word_repeated(text: str, threshold: int = 5) -> bool:
    """Return True if text is a single word repeated >= threshold times.

    Guards against the Credgenics STT occasionally returning a hallucinated
    repetition (e.g. "हाँ हाँ हाँ हाँ हाँ") for noise/silence.
    """
    words = text.split()
    clean_words = [w.lower().strip(string.punctuation) for w in words]
    return len(set(clean_words)) == 1 and len(clean_words) >= threshold


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

    @staticmethod
    def _stt_lang_iso(frame: TranscriptionFrame) -> Optional[str]:
        """Reduce frame.language (Pipecat enum or string) to a bare ISO code."""
        lang = frame.language
        if not lang:
            return None
        raw = lang.value if isinstance(lang, PipecatLanguage) else str(lang)
        return raw.split("-", 1)[0].lower() if raw else None

    def _maybe_commit(self) -> None:
        s = self._state
        if s.candidate_language != s.current_language and s.candidate_count >= LANG_SWITCH_THRESHOLD:
            old = s.current_language
            new = commit_switch(s)
            logger.info("language switch: %s -> %s at turn %d", old, new, s.turn_count)
            if self._on_switch is not None:
                try:
                    self._on_switch(old, new)
                except Exception as exc:
                    logger.warning("on_language_switch raised: %s", exc)

    def _inject(self, frame: TranscriptionFrame, *, note: str = "") -> None:
        if not self._inject_suffix:
            return
        s = self._state
        suffix = SUPPORTED_LNG_SUFFIX.get(s.current_language, "")
        if not suffix:
            return
        logger.info(
            "llm_suffix_injected | lang=%s | suffix=%r%s",
            s.current_language,
            suffix,
            f" ({note})" if note else "",
        )
        frame.text = f"{frame.text}{suffix}"

    def _handle(self, frame: TranscriptionFrame) -> None:
        import time
        t0 = time.perf_counter()
        s = self._state
        s.turn_count += 1

        stt_iso = self._stt_lang_iso(frame)
        logger.info(
            "stt_language | stt_lang=%s | current_lang=%s | turn=%d",
            stt_iso or "n/a",
            s.current_language,
            s.turn_count,
        )
        t_after_log = time.perf_counter()

        # A valid transcript clears the Credgenics STT invalid-language strikes.
        s.invalid_language_count = 0

        # Single-word repetition (Credgenics STT noise artifact): don't vote on
        # language or inject a suffix for it.
        if STT_PRIMARY in ("credgenics", "credgenics_http") and _is_single_word_repeated(frame.text.strip()):
            logger.info("single_word_repetition | text=%r | skipping language logic", frame.text)
            return

        # Short utterances (≤3 words) are unreliable for language detection —
        # both STT and text-level LID frequently misclassify them on Hinglish
        # ("Yes, madam.", "haan ji"). Skip all switching logic; keep the
        # current language and just inject its suffix.
        is_short = len(frame.text.split()) <= 3
        if is_short:
            self._inject(frame, note="short-utterance")
            logger.info(
                "lsp_timing | total=%.1fms | path=short_no_switch",
                (time.perf_counter() - t0) * 1000,
            )
            return

        # Vote for the STT-reported language first — it's a strong signal that
        # we should not override with a slower text-level LID guess.
        if stt_iso and stt_iso in s.supported_languages:
            update_candidate(s, stt_iso)
            self._maybe_commit()
        t_after_commit = time.perf_counter()

        # Text-level LID fallback: only fires if neither the STT nor the
        # SpeechBrain LIDProcessor have proposed a candidate yet.
        # t_langdetect = 0.0
        # if not s.candidate_language:
        #     t_ld0 = time.perf_counter()
        #     fallback = detect_language_from_text(frame.text)
        #     t_langdetect = (time.perf_counter() - t_ld0) * 1000
        #     if fallback and fallback in s.supported_languages:
        #         update_candidate(s, fallback)
        #         self._maybe_commit()

        self._inject(frame)
        logger.info(
            "lsp_timing | total=%.1fms | commit=%.1fms ",
            (time.perf_counter() - t0) * 1000,
            (t_after_commit - t_after_log) * 1000,
        )
