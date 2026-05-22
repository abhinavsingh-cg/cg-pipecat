"""
VoicebotMetricsObserver — records pipeline timing to OTel metrics.

Instruments created (all times in ms):
  voicebot.service_ttfb_ms     histogram  time-to-first-token/packet per service
                                           labels: service_type, {stt|llm|tts}_vendor
                                           STT: VADUserStoppedSpeaking → UserStoppedSpeaking
                                               (flush trigger → Sarvam END_SPEECH confirmation)
                                           LLM/TTS: TTFBMetricsData from pipecat
  bot.msg_turnaround_time      histogram  UserStoppedSpeaking → BotStartedSpeaking; label: language
  voicebot.active_calls        updowncounter  active calls right now

All instruments are no-op when no MeterProvider is configured (i.e. when
OTEL_EXPORTER_OTLP_ENDPOINT is unset and _setup_otel_metrics() was not called).
"""
from __future__ import annotations

import time

from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    CancelFrame,
    EndFrame,
    MetricsFrame,
    TranscriptionFrame,
    UserStoppedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.metrics.metrics import TTFBMetricsData
from pipecat.observers.base_observer import BaseObserver, FramePushed

from voicebot.config import STT_PRIMARY

# Ordered list of (substring, vendor_label) for extracting vendor from class name.
_VENDOR_PREFIXES = [
    ("sarvam", "sarvam"),
    ("groq", "groq"),
    ("deepgram", "deepgram"),
    ("bedrock", "bedrock"),
    ("aws", "bedrock"),
    ("openai", "openai"),
    ("elevenlabs", "elevenlabs"),
    ("cartesia", "cartesia"),
    ("credgenics", "credgenics"),
]


def _vendor(processor: str) -> str:
    name = processor.lower()
    for token, vendor in _VENDOR_PREFIXES:
        if token in name:
            return vendor
    return "unknown"


def _service_type(processor: str) -> str:
    p = processor.upper()
    if "STT" in p:
        return "stt"
    if "LLM" in p:
        return "llm"
    if "TTS" in p:
        return "tts"
    return "other"


class VoicebotMetricsObserver(BaseObserver):
    """Records voicebot-specific latency and capacity metrics via OTel."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._stt_request_at: float = 0.0
        self._stt_vendor: str = _vendor(STT_PRIMARY or "")
        self._user_stopped_at: float = 0.0
        self._language: str = "unknown"
        self._call_counted: bool = False

        try:
            from opentelemetry import metrics
            meter = metrics.get_meter("voicebot")
            self._service_ttfb = meter.create_histogram(
                "voicebot.service_ttfb_ms",
                unit="ms",
                description=(
                    "Time-to-first-token/packet per pipeline service. "
                    "Labels: service_type (stt/llm/tts), {stt|llm|tts}_vendor."
                ),
            )
            self._bot_turnaround = meter.create_histogram(
                "bot.msg_turnaround_time",
                unit="ms",
                description="Time from end of user speech to first audio frame sent to caller",
            )
            self._active_calls = meter.create_up_down_counter(
                "voicebot.active_calls",
                description="Number of currently active calls (open RTP sockets)",
            )
        except Exception:
            self._service_ttfb = None
            self._bot_turnaround = None
            self._active_calls = None

    async def on_push_frame(self, data: FramePushed) -> None:
        frame = data.frame

        if isinstance(frame, TranscriptionFrame):
            # Language detection only — STT TTFB is measured via VADUserStopped→UserStopped.
            if frame.language is not None:
                self._language = (
                    frame.language.value if hasattr(frame.language, "value") else str(frame.language)
                )

        elif isinstance(frame, VADUserStoppedSpeakingFrame):
            # Flush is triggered at this moment — start the STT finalization clock.
            self._stt_request_at = time.monotonic()

        elif isinstance(frame, UserStoppedSpeakingFrame):
            # Sarvam sends END_SPEECH after processing the flush; record STT TTFB here.
            if self._stt_request_at and self._service_ttfb is not None:
                elapsed_ms = (time.monotonic() - self._stt_request_at) * 1000
                self._service_ttfb.record(
                    elapsed_ms, {"service_type": "stt", "stt_vendor": self._stt_vendor}
                )
            self._stt_request_at = 0.0
            # Also start the bot turnaround clock from the same confirmed-turn-end event.
            self._user_stopped_at = time.monotonic()

        elif isinstance(frame, BotStartedSpeakingFrame):
            if self._user_stopped_at and self._bot_turnaround is not None:
                elapsed_ms = (time.monotonic() - self._user_stopped_at) * 1000
                self._bot_turnaround.record(elapsed_ms, {"language": self._language})
            self._user_stopped_at = 0.0

        elif isinstance(frame, MetricsFrame) and self._service_ttfb is not None:
            for m in frame.data:
                if isinstance(m, TTFBMetricsData):
                    stype = _service_type(m.processor)
                    if stype != "stt" and m.value > 0:
                        self._service_ttfb.record(
                            m.value * 1000,
                            {"service_type": stype, f"{stype}_vendor": _vendor(m.processor)},
                        )

        elif isinstance(frame, (EndFrame, CancelFrame)) and self._active_calls is not None:
            if self._call_counted:
                self._call_counted = False
                self._active_calls.add(-1)

    async def on_pipeline_started(self) -> None:
        if self._active_calls is not None:
            self._call_counted = True
            self._active_calls.add(1)
