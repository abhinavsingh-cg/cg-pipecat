"""
TTS factory — selector returning a stock Pipecat TTSService.
"""
from __future__ import annotations

import logging

from pipecat.services.tts_service import TTSService
from pipecat.transcriptions.language import Language as PipecatLanguage

from voicebot.config import (
    CARTESIA_API_KEY,
    CARTESIA_VOICE_ID,
    ELEVENLABS_VOICE_ID,
    ELEVEN_API_KEY,
    LANG_TO_ISO,
    SAMPLE_RATE,
    SARVAM_API_KEY,
    SARVAM_TTS_MODEL,
    SARVAM_TTS_PACE,
    SARVAM_TTS_SPEAKER,
    SARVAM_TTS_TEMPERATURE,
    TTS_VENDOR,
)

logger = logging.getLogger(__name__)


def _pipecat_language(internal_key: str) -> PipecatLanguage:
    iso = LANG_TO_ISO.get(internal_key, "hi")
    try:
        return PipecatLanguage(iso)
    except ValueError:
        return PipecatLanguage.HI


def build_tts(language_key: str) -> TTSService:
    vendor = TTS_VENDOR
    if vendor == "sarvam":
        from pipecat.services.sarvam.tts import SarvamTTSService  # type: ignore
        return SarvamTTSService(
            api_key=SARVAM_API_KEY,
            model=SARVAM_TTS_MODEL,
            voice_id=SARVAM_TTS_SPEAKER,
            sample_rate=SAMPLE_RATE,
            params=SarvamTTSService.InputParams(
                language=_pipecat_language(language_key),
                pace=SARVAM_TTS_PACE,
                temperature=SARVAM_TTS_TEMPERATURE,
            ),
        )
    if vendor == "elevenlabs":
        from pipecat.services.elevenlabs.tts import ElevenLabsTTSService  # type: ignore
        return ElevenLabsTTSService(
            api_key=ELEVEN_API_KEY,
            voice_id=ELEVENLABS_VOICE_ID,
        )
    if vendor == "cartesia":
        from pipecat.services.cartesia.tts import CartesiaTTSService  # type: ignore
        iso = LANG_TO_ISO.get(language_key, "hi")
        return CartesiaTTSService(
            api_key=CARTESIA_API_KEY,
            voice_id=CARTESIA_VOICE_ID,
            language=iso,
        )
    raise ValueError(f"Unknown TTS_VENDOR={vendor!r}")
