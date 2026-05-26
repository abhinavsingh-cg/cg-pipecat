"""
STT factory — returns the configured primary STT service based on STT_PRIMARY.

TO ADD A NEW VENDOR:
  1. pip install "pipecat-ai[your-vendor]"
  2. Add a new `if vendor == "yourvendor":` block below.
  3. Set STT_PRIMARY=yourvendor in .env.
  4. Streaming STTs (Sarvam, Deepgram) attach language metadata to frames
     automatically, so inject_suffix stays False. Non-streaming STTs that
     don't return language metadata should set inject_suffix=True in pipeline.py.

HOW STT FITS INTO THE PIPELINE:
  The STT service sits after LID and before LanguageSuffixProcessor.
  It receives InputAudioRawFrame (gated by VAD from context_aggr.user()) and
  emits TranscriptionFrame(text=..., language=...) downstream.
  Streaming STTs (Sarvam, Deepgram) also emit InterimTranscriptionFrames
  which the LLM aggregator can use to cancel early.
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
    """
    Returns the configured STT service.

    `state` is passed through so services that need the current/supported
    language at runtime (Credgenics HTTP STT) can read it live.
    """
    vendor = STT_PRIMARY
    if vendor == "sarvam":
        import time, logging as _lg
        _log = _lg.getLogger("voicebot")
        from pipecat.services.sarvam.stt import SarvamSTTService  # type: ignore
        t0 = time.perf_counter()
        svc = SarvamSTTService(
            api_key=SARVAM_API_KEY,
            model=SARVAM_STT_MODEL,
            mode="transcribe",
            sample_rate=SARVAM_STT_SAMPLE_RATE,
            vad_signals = True,
            params=SarvamSTTService.InputParams(
                language=None,
                vad_signals=True,
                high_vad_sensitivity=True,   # cut server-side END_SPEECH lag
            )
        )
        _log.info("sarvam_stt_init | %.0fms", (time.perf_counter() - t0) * 1000)

        # Tag every UserStartedSpeakingFrame / UserStoppedSpeakingFrame that
        # Sarvam itself broadcasts (from its events-channel END_SPEECH /
        # START_SPEECH signals), so we can tell them apart from the strategy's
        # trigger_user_turn_stopped() emissions in the log.
        from pipecat.frames.frames import (
            UserStartedSpeakingFrame as _UStart,
            UserStoppedSpeakingFrame as _UStop,
        )
        _orig_broadcast = svc.broadcast_frame
        async def _tagged_broadcast(frame_cls, *args, **kwargs):
            try:
                if frame_cls is _UStop:
                    _log.info("sarvam_emit | UserStoppedSpeakingFrame (END_SPEECH)")
                elif frame_cls is _UStart:
                    _log.info("sarvam_emit | UserStartedSpeakingFrame (START_SPEECH)")
            except Exception:
                pass
            return await _orig_broadcast(frame_cls, *args, **kwargs)
        svc.broadcast_frame = _tagged_broadcast
        return svc
    if vendor == "deepgram":
        from pipecat.services.deepgram.stt import DeepgramSTTService, LiveOptions  # type: ignore
        # Pipecat 1.1.0 moved model/language into LiveOptions; top-level kwargs
        # like `model=` and `detect_language=` are silently ignored and the
        # service falls back to nova-3-general + Language.EN. For nova-3
        # multilingual transcription, set language="multi" (Deepgram replaced
        # `detect_language` with this for nova-3).
        return DeepgramSTTService(
            api_key=DEEPGRAM_API_KEY,
            live_options=LiveOptions(
                model=DEEPGRAM_STT_MODEL,
                language="multi",
                # smart_format=True,
                # punctuate=True,
                interim_results=True,
            ),
        )
    if vendor in ("credgenics", "credgenics_http"):
        # Internal non-streaming STT — see stt/credgenics_http.py for the
        # POST /transcribe protocol. Requires STT_BASE_URL in .env.
        return CredgenicsHTTPSTTService(state=state)
    raise ValueError(f"Unknown STT_PRIMARY={vendor!r}")
