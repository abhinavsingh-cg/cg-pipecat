"""
LatencyLogger — emits searchable, component-tagged latency logs.

Format:
  || COMPONENT || event=<name> value_ms=<float> call_id=<id> [extra=...]

Grep-friendly keywords: || VAD ||, || STT ||, || LLM ||, || TTS ||,
|| BOT ||, || E2E ||, || INTR ||.

Place at the end of the pipeline (after the second EventLogger) so it sees
every frame in both directions. Pure observer — only re-pushes the frame.
"""
from __future__ import annotations

import logging
import time
from typing import Optional

from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    Frame,
    InterruptionFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
    MetricsFrame,
    TranscriptionFrame,
    TTSStartedFrame,
    TTSStoppedFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.metrics.metrics import TTFBMetricsData

try:
    from pipecat.metrics.metrics import ProcessingMetricsData
except ImportError:  # older pipecat versions
    ProcessingMetricsData = None  # type: ignore[assignment]

try:
    from pipecat.metrics.metrics import LLMUsageMetricsData
except ImportError:
    LLMUsageMetricsData = None  # type: ignore[assignment]
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from voicebot.state.language_state import LanguageState

logger = logging.getLogger("voicebot.latency")


def _now() -> float:
    return time.monotonic()


class LatencyLogger(FrameProcessor):
    def __init__(self, state: LanguageState):
        super().__init__()
        self._state = state
        # per-turn timestamps (monotonic seconds)
        self._vad_user_start: Optional[float] = None
        self._vad_user_stop: Optional[float] = None
        self._user_stop: Optional[float] = None
        self._llm_start: Optional[float] = None
        self._first_llm_text: Optional[float] = None
        self._tts_start: Optional[float] = None
        self._bot_start: Optional[float] = None

    # ── helpers ──────────────────────────────────────────────────────────────
    def _log(self, component: str, event: str, value_ms: Optional[float] = None, **extra) -> None:
        parts = [f"|| {component} ||", f"event={event}", f"call_id={self._state.call_id}"]
        if value_ms is not None:
            parts.append(f"value_ms={value_ms:.1f}")
        for k, v in extra.items():
            parts.append(f"{k}={v}")
        logger.info(" ".join(parts))

    def _delta_ms(self, start: Optional[float]) -> Optional[float]:
        if start is None:
            return None
        return (_now() - start) * 1000.0

    def _reset_turn(self) -> None:
        self._vad_user_start = None
        self._vad_user_stop = None
        self._user_stop = None
        self._llm_start = None
        self._first_llm_text = None
        self._tts_start = None
        self._bot_start = None

    # ── main entry ───────────────────────────────────────────────────────────
    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        try:
            self._handle(frame)
        except Exception as exc:
            logger.warning("latency_logger error: %s", exc)
        await self.push_frame(frame, direction)

    def _handle(self, frame: Frame) -> None:
        now = _now()

        # ── VAD ──
        if isinstance(frame, VADUserStartedSpeakingFrame):
            self._vad_user_start = now
            self._log("VAD", "user_started")
            return
        if isinstance(frame, VADUserStoppedSpeakingFrame):
            self._vad_user_stop = now
            self._log(
                "VAD",
                "user_stopped",
                value_ms=self._delta_ms(self._vad_user_start),
                metric="user_speech_duration",
            )
            return

        # ── STT ──
        if isinstance(frame, UserStartedSpeakingFrame):
            self._log("STT", "user_started_confirmed")
            return
        if isinstance(frame, UserStoppedSpeakingFrame):
            self._user_stop = now
            self._log(
                "STT",
                "user_stopped_confirmed",
                value_ms=self._delta_ms(self._vad_user_stop),
                metric="stt_finalize",
            )
            return
        if isinstance(frame, TranscriptionFrame):
            lang = getattr(frame, "language", None)
            text = (getattr(frame, "text", "") or "")[:80]
            self._log("STT", "transcript", language=lang, text=repr(text))
            return

        # ── LLM ──
        if isinstance(frame, LLMFullResponseStartFrame):
            self._llm_start = now
            self._first_llm_text = None
            self._log(
                "LLM",
                "response_start",
                value_ms=self._delta_ms(self._user_stop),
                metric="llm_ttfb_pipeline_edge",
            )
            return
        if isinstance(frame, LLMTextFrame):
            if self._first_llm_text is None:
                self._first_llm_text = now
                self._log(
                    "LLM",
                    "first_text",
                    value_ms=self._delta_ms(self._llm_start),
                    metric="llm_first_text_after_start",
                )
            return
        if isinstance(frame, LLMFullResponseEndFrame):
            self._log(
                "LLM",
                "response_end",
                value_ms=self._delta_ms(self._llm_start),
                metric="llm_total_stream",
            )
            return

        # ── TTS ──
        if isinstance(frame, TTSStartedFrame):
            self._tts_start = now
            self._log(
                "TTS",
                "started",
                value_ms=self._delta_ms(self._first_llm_text),
                metric="tts_ttfb_pipeline_edge",
            )
            return
        if isinstance(frame, TTSStoppedFrame):
            self._log(
                "TTS",
                "stopped",
                value_ms=self._delta_ms(self._tts_start),
                metric="tts_synth_duration",
            )
            return

        # ── BOT speech / E2E turnaround ──
        if isinstance(frame, BotStartedSpeakingFrame):
            self._bot_start = now
            self._log(
                "TTS",
                "audio_first_play",
                value_ms=self._delta_ms(self._tts_start),
                metric="tts_to_playback",
            )
            self._log(
                "E2E",
                "turnaround",
                value_ms=self._delta_ms(self._user_stop),
                metric="user_stop_to_bot_start",
            )
            return
        if isinstance(frame, BotStoppedSpeakingFrame):
            self._log(
                "BOT",
                "stopped",
                value_ms=self._delta_ms(self._bot_start),
                metric="bot_speech_duration",
            )
            # turn complete → reset for next turn
            self._reset_turn()
            return

        # ── Interruption ──
        if isinstance(frame, InterruptionFrame):
            self._log("INTR", "interruption")
            self._reset_turn()
            return

        # ── Vendor-reported metrics from Pipecat ──
        if isinstance(frame, MetricsFrame):
            for m in frame.data:
                if isinstance(m, TTFBMetricsData):
                    component = self._service_from_processor(m.processor)
                    self._log(
                        component,
                        "ttfb_vendor",
                        value_ms=m.value * 1000.0,
                        processor=m.processor,
                        metric="vendor_ttfb",
                    )
                elif ProcessingMetricsData is not None and isinstance(m, ProcessingMetricsData):
                    component = self._service_from_processor(m.processor)
                    self._log(
                        component,
                        "processing_vendor",
                        value_ms=m.value * 1000.0,
                        processor=m.processor,
                        metric="vendor_processing",
                    )
                elif LLMUsageMetricsData is not None and isinstance(m, LLMUsageMetricsData):
                    self._log(
                        "LLM",
                        "tokens",
                        processor=m.processor,
                        prompt_tokens=getattr(m.value, "prompt_tokens", None),
                        completion_tokens=getattr(m.value, "completion_tokens", None),
                    )

    @staticmethod
    def _service_from_processor(processor: str) -> str:
        p = (processor or "").upper()
        if "STT" in p:
            return "STT"
        if "LLM" in p:
            return "LLM"
        if "TTS" in p:
            return "TTS"
        return "MISC"
