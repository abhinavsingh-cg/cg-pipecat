"""
CredgenicsHTTPSTT — non-streaming Pipecat STTService for the internal
/transcribe endpoint.

Behavior ported from voice_bot.py:speech_to_text (lines 246-296):
  POST {STT_BASE_URL}/transcribe
    form fields:
      audio:               raw 16-bit PCM (8 kHz mono)
      current_language:    e.g. "hindi"
      supported_languages: repeated; one per call-supported language
    response (JSON):
      { "transcript": "...", "language": "hindi" }
"""
from __future__ import annotations

import logging
from typing import AsyncGenerator, List, Optional

import aiohttp

from pipecat.frames.frames import ErrorFrame, Frame, TranscriptionFrame
from pipecat.services.stt_service import STTService
from pipecat.transcriptions.language import Language as PipecatLanguage

from voicebot.config import LANGUAGE_TO_CODES, STT_BASE_URL, STT_TIMEOUT_S
from voicebot.state.language_state import LanguageState

logger = logging.getLogger(__name__)


class CredgenicsHTTPSTTService(STTService):
    """
    Non-streaming STT — Pipecat's STTService base buffers audio between
    UserStartedSpeakingFrame and UserStoppedSpeakingFrame, then calls
    run_stt(audio_bytes), which yields TranscriptionFrame(s).

    Construction-time params:
      state: shared LanguageState (current_language, supported_languages).
      base_url: defaults to STT_BASE_URL env var.
    """

    def __init__(
        self,
        state: LanguageState,
        *,
        base_url: Optional[str] = None,
        timeout_s: float = STT_TIMEOUT_S,
        sample_rate: int = 8000,
        **kwargs,
    ):
        super().__init__(sample_rate=sample_rate, **kwargs)
        self._state = state
        self._base_url = (base_url or STT_BASE_URL).rstrip("/")
        self._timeout = aiohttp.ClientTimeout(total=timeout_s)
        if not self._base_url:
            logger.warning("CredgenicsHTTPSTT: STT_BASE_URL is empty — service will error on use")

    async def run_stt(self, audio: bytes) -> AsyncGenerator[Frame, None]:
        if not audio:
            return
        if not self._base_url:
            yield ErrorFrame(error="CredgenicsHTTPSTT: STT_BASE_URL not configured")
            return

        form = aiohttp.FormData()
        form.add_field("audio", audio, content_type="application/octet-stream", filename="audio.pcm")
        form.add_field("current_language", self._state.current_language)
        for lang in self._state.supported_languages:
            iso = LANGUAGE_TO_CODES.get(lang, lang)
            form.add_field("supported_languages", iso)

        url = f"{self._base_url}/transcribe"
        try:
            async with aiohttp.ClientSession(timeout=self._timeout) as session:
                async with session.post(url, data=form) as resp:
                    if resp.status != 200:
                        body = await resp.text()
                        yield ErrorFrame(error=f"CredgenicsHTTPSTT HTTP {resp.status}: {body[:200]}")
                        return
                    payload = await resp.json()
        except Exception as exc:
            yield ErrorFrame(error=f"CredgenicsHTTPSTT request failed: {exc}")
            return

        transcript = (payload.get("transcript") or "").strip()
        if not transcript:
            return
        detected = payload.get("language") or self._state.current_language
        # finalized=True short-circuits SpeechTimeoutUserTurnStopStrategy's
        # stt_timeout safety net. Without it, the strategy emits a second
        # UserStoppedSpeakingFrame when stt_timeout elapses, even though the
        # transcript already arrived — causing duplicate turn-stop events.
        yield TranscriptionFrame(
            text=transcript,
            user_id="",
            timestamp="",
            language=_pipecat_language(detected),
            finalized=True,
        )


def _pipecat_language(internal_key: str) -> Optional[PipecatLanguage]:
    iso = LANGUAGE_TO_CODES.get(internal_key)
    if not iso:
        return None
    try:
        return PipecatLanguage(iso)
    except ValueError:
        return None
