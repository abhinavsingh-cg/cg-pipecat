"""
STT factory — returns the configured primary STT service.
"""
from __future__ import annotations

import logging

from pipecat.services.stt_service import STTService
from pipecat.transcriptions.language import Language as PipecatLanguage

from voicebot.config import (
    DEEPGRAM_API_KEY,
    DEEPGRAM_STT_MODEL,
    LANGUAGE_TO_CODES,
    SARVAM_API_KEY,
    SARVAM_STT_MODEL,
    SARVAM_STT_SAMPLE_RATE,
    STT_PRIMARY,
)
from voicebot.state.language_state import LanguageState
from voicebot.stt.credgenics_http import CredgenicsHTTPSTTService

logger = logging.getLogger(__name__)


def _pipecat_language(internal_key: str):
    iso = LANGUAGE_TO_CODES.get(internal_key, "en")
    try:
        return PipecatLanguage(iso)
    except ValueError:
        return None


def build_stt(state: LanguageState) -> STTService:
    vendor = STT_PRIMARY
    if vendor == "sarvam":
        from pipecat.services.sarvam.stt import SarvamSTTService  # type: ignore
        # language=None → Sarvam server-side auto-detect.
        return SarvamSTTService(
            api_key=SARVAM_API_KEY,
            model=SARVAM_STT_MODEL,
            mode="transcribe",
            sample_rate=SARVAM_STT_SAMPLE_RATE,
            params=SarvamSTTService.InputParams(language=None),
        )
    if vendor == "deepgram":
        from pipecat.services.deepgram.stt import DeepgramSTTService  # type: ignore
        return DeepgramSTTService(
            api_key=DEEPGRAM_API_KEY,
            model=DEEPGRAM_STT_MODEL,
            language=_pipecat_language(state.current_language),
        )
    if vendor in ("credgenics", "credgenics_http"):
        return CredgenicsHTTPSTTService(state=state)
    raise ValueError(f"Unknown STT_PRIMARY={vendor!r}")
