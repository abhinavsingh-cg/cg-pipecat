"""
CredgenicsHTTPSTT — non-streaming Pipecat STT for the internal /transcribe endpoint.

Subclasses Pipecat's SegmentedSTTService, which buffers audio between
VADUserStartedSpeakingFrame / VADUserStoppedSpeakingFrame (pushed UPSTREAM by the
user aggregator's VAD controller), packages it into a WAV, and calls run_stt()
ONCE per utterance. This avoids sending 20 ms chunks to the HTTP endpoint.

Pipeline requirement (see pipeline.py): the user aggregator must run the Silero
VAD (vad_analyzer=...) — it already does here — and for the non-streaming path the
turn-stop strategy must be SpeechTimeoutUserTurnStopStrategy so the turn waits for
this STT's (finalized) transcript before firing the LLM.

API contract (matches the staging curl):
  POST {STT_BASE_URL}/transcribe?call_id=..&campaign_id=..&current_language=hindi
    headers: authenticationtoken: STT_API_KEY
    multipart form:
      file:                WAV (16-bit PCM, 8 kHz mono) — built by SegmentedSTTService
      current_language:    full language name, e.g. "hindi"
      supported_languages: repeated ISO codes, one per supported language (hi, en)
  response (JSON):
      { "transcription": "...", "language": "en" | "invalid", "latency": 0.68, ... }
"""
from __future__ import annotations

import logging
from typing import AsyncGenerator, Optional

import aiohttp

from pipecat.frames.frames import ErrorFrame, Frame, TranscriptionFrame
from pipecat.services.settings import STTSettings
from pipecat.services.stt_service import SegmentedSTTService
from pipecat.transcriptions.language import Language as PipecatLanguage

from voicebot.config import (
    LANGUAGE_CODES,
    LANGUAGE_TO_CODES,
    STT_API_KEY,
    STT_BASE_URL,
    STT_TIMEOUT_S,
)
from voicebot.state.language_state import LanguageState

logger = logging.getLogger(__name__)

# P99 latency (s) from VAD stop to final transcript. Sizes the stt_timeout safety
# net in SpeechTimeoutUserTurnStopStrategy. Our transcript is finalized=True so the
# safety net is short-circuited on arrival; this is just the upper-bound wait.
_TTFS_P99_LATENCY_S = 1.0


class CredgenicsHTTPSTTService(SegmentedSTTService):
    """Non-streaming STT. run_stt() receives a full-utterance WAV (one call/turn)."""

    def __init__(
        self,
        state: LanguageState,
        *,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        timeout_s: float = STT_TIMEOUT_S,
        sample_rate: int = 8000,
        **kwargs,
    ):
        super().__init__(
            sample_rate=sample_rate,
            ttfs_p99_latency=_TTFS_P99_LATENCY_S,
            # This service auto-detects language server-side and has no model knob;
            # initialize both to None so STTSettings.validate_complete passes.
            settings=STTSettings(model=None, language=None),
            **kwargs,
        )
        self._state = state
        self._base_url = (base_url or STT_BASE_URL).rstrip("/")
        self._api_key = api_key or STT_API_KEY
        self._timeout = aiohttp.ClientTimeout(total=timeout_s)
        if not self._base_url:
            logger.warning("CredgenicsHTTPSTT: STT_BASE_URL is empty — service will error on use")

    def _current_language_name(self) -> str:
        """state.current_language is an ISO code (e.g. 'hi'); the endpoint expects the
        full name (e.g. 'hindi')."""
        return LANGUAGE_CODES.get(self._state.current_language, self._state.current_language)

    async def run_stt(self, audio: bytes) -> AsyncGenerator[Frame, None]:
        # `audio` is already a WAV container built by SegmentedSTTService.
        if not audio:
            return
        if not self._base_url:
            yield ErrorFrame(error="CredgenicsHTTPSTT: STT_BASE_URL not configured")
            return

        current_language = self._current_language_name()

        form = aiohttp.FormData()
        form.add_field("file", audio, filename="audio.wav", content_type="audio/wav")
        form.add_field("current_language", current_language)
        for lang in self._state.supported_languages:
            iso = lang if lang in LANGUAGE_CODES else LANGUAGE_TO_CODES.get(lang, lang)
            form.add_field("supported_languages", iso)

        url = f"{self._base_url}/transcribe"
        params = {
            "call_id": self._state.call_id,
            "campaign_id": self._state.campaign_id,
            "current_language": current_language,
        }
        headers = {"authenticationtoken": self._api_key} if self._api_key else {}

        logger.info(
            "stt_request | call_id=%s | lang=%s | wav_bytes=%d",
            self._state.call_id, current_language, len(audio),
        )

        try:
            async with aiohttp.ClientSession(timeout=self._timeout) as session:
                async with session.post(url, params=params, data=form, headers=headers) as resp:
                    # Endpoint returns 200 or 201 on success.
                    if not (200 <= resp.status < 300):
                        body = await resp.text()
                        yield ErrorFrame(error=f"CredgenicsHTTPSTT HTTP {resp.status}: {body[:200]}")
                        return
                    payload = await resp.json()
        except Exception as exc:
            yield ErrorFrame(error=f"CredgenicsHTTPSTT request failed: {exc}")
            return

        logger.info("stt_response | payload=%s", payload)

        detected = (payload.get("language") or "").strip().lower()

        # Endpoint signals an unsupported / undetectable language. Don't emit a
        # transcript — signal via ErrorFrame. (LanguageSuffixProcessor resets the
        # strike counter on the next valid transcript.)
        if detected == "invalid":
            logger.warning("stt_invalid_language | call_id=%s", self._state.call_id)
            yield ErrorFrame(error="STT_INVALID_LANGUAGE")
            return

        transcript = (payload.get("transcription") or "").strip()
        if not transcript:
            return

        try:
            lang_enum = PipecatLanguage(detected) if detected else None
        except ValueError:
            lang_enum = None

        # finalized is set True by SegmentedSTTService.push_frame.
        yield TranscriptionFrame(text=transcript, user_id="", timestamp="", language=lang_enum)
