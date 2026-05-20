"""
TTS factory — returns a stock Pipecat TTSService based on TTS_VENDOR.

TO ADD A NEW VENDOR:
  1. pip install "pipecat-ai[your-vendor]"
  2. Add a new `if vendor == "yourvendor":` block below.
  3. Set TTS_VENDOR=yourvendor in .env.
  4. Add any API key vars to config.py.

LANGUAGE SWITCHING: when the user switches language, pipeline.py calls
tts.update_options(target_language_code=...) or tts.update_options(language=...).
Ensure your vendor's TTSService supports one of these kwargs, or extend
on_language_switch() in pipeline.py to handle vendor-specific update calls.
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
    """Map internal language key (e.g. "hindi") → Pipecat Language enum ("hi")."""
    iso = LANG_TO_ISO.get(internal_key, "hi")
    try:
        return PipecatLanguage(iso)
    except ValueError:
        return PipecatLanguage.HI


def build_tts(language_key: str) -> TTSService:
    """
    Returns the configured TTS service, initialized for `language_key`.

    language_key is an internal key like "hindi", "english", "tamil".
    The factory converts it to the vendor-specific format (ISO code, Pipecat
    enum, etc.) as needed.
    """
    vendor = TTS_VENDOR
    iso = LANG_TO_ISO.get(language_key, "hi")
    logger.info(
        "tts_language_init | vendor=%s | language_key=%s | iso=%s",
        vendor,
        language_key,
        iso,
    )
    if vendor == "sarvam":
        from pipecat.services.sarvam.tts import SarvamTTSService  # type: ignore
        # voice_id / speaker options: shubh, meera, arvind, amol, amartya, diya
        # See SARVAM_TTS_SPEAKER in config.py to change the default voice.
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
        # ElevenLabs doesn't natively support language switching — the voice
        # model determines the language. For multilingual calls, pick a
        # multilingual voice (e.g. "eleven_multilingual_v2").
        # return ElevenLabsTTSService(
        #     model = "eleven_flash_v2_5",
        #     api_key=ELEVEN_API_KEY,
        #     voice_id=ELEVENLABS_VOICE_ID,
        # )
        return ElevenLabsTTSService(
            model="eleven_flash_v2_5",
            api_key=ELEVEN_API_KEY,
            voice_id=ELEVENLABS_VOICE_ID,
            params=ElevenLabsTTSService.InputParams(
                chunk_length_schedule=[10, 90, 120, 150],  # or even [30, 60, ...]
            ),  
        )    
    if vendor == "cartesia":
        from pipecat.services.cartesia.tts import CartesiaTTSService  # type: ignore
        return CartesiaTTSService(
            api_key=CARTESIA_API_KEY,
            voice_id=CARTESIA_VOICE_ID,
            language=iso,
        )
    raise ValueError(f"Unknown TTS_VENDOR={vendor!r}")
