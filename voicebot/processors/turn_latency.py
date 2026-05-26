"""
TurnLatencyTracker — logs one summary line per user turn.

Stages measured (all in ms from UserStoppedSpeakingFrame = T0):
  vad+turn  : VADUserStoppedSpeakingFrame → UserStoppedSpeakingFrame
              (smart-turn inference + any aggregator gating)
  stt       : UserStoppedSpeakingFrame   → TranscriptionFrame
  llm_ttfb  : TranscriptionFrame         → LLMFullResponseStartFrame
  llm_total : LLMFullResponseStartFrame  → LLMFullResponseEndFrame
  tts_ttfb  : LLMFullResponseStartFrame  → first TTSAudioRawFrame
  bot_start : first TTSAudioRawFrame     → BotStartedSpeakingFrame
  TOTAL     : UserStoppedSpeakingFrame   → first TTSAudioRawFrame
"""
from __future__ import annotations

import logging
import time

from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    Frame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    TTSAudioRawFrame,
    UserStoppedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

logger = logging.getLogger("voicebot.turn_latency")


class _Shared:
    """Shared between LLMTimingProbe (placed right after the LLM, before TTS)
    and TurnLatencyTracker (at end of pipeline). The probe captures LLM
    start/end timestamps BEFORE the TTS service holds the End frame as a
    flush signal — otherwise the End frame doesn't reach the end of the
    pipeline until TTS has finished synthesizing all audio, which makes the
    'llm_stream' measurement meaningless.
    """
    t_llm_start: float | None = None
    t_llm_end: float | None = None


class LLMTimingProbe(FrameProcessor):
    """Insert immediately after the LLM service. Records the true LLM
    stream start/end into the shared state."""

    def __init__(self, shared: _Shared) -> None:
        super().__init__()
        self._shared = shared

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if isinstance(frame, LLMFullResponseStartFrame):
            self._shared.t_llm_start = time.perf_counter()
            self._shared.t_llm_end = None
        elif isinstance(frame, LLMFullResponseEndFrame):
            self._shared.t_llm_end = time.perf_counter()
        await self.push_frame(frame, direction)


class TurnLatencyTracker(FrameProcessor):
    def __init__(self, shared: _Shared | None = None) -> None:
        super().__init__()
        self._shared = shared or _Shared()
        self._reset()

    def _reset(self) -> None:
        self.t_vad_stop = None
        self.t_user_stop = None
        self.t_tts_first = None
        self.t_bot_start = None
        self._logged = False
        # Don't reset _shared.t_llm_* here — the probe upstream owns those.

    @staticmethod
    def _ms(a, b) -> str:
        if a is None or b is None:
            return "—"
        return f"{(b - a) * 1000:.0f}"

    def _maybe_log(self) -> None:
        if self._logged:
            return
        if self.t_bot_start is None or self._shared.t_llm_end is None:
            return
        self._logged = True
        # tts_lag is signed: negative = TTS streamed audio BEFORE the LLM
        # finished (good); positive = TTS waited for the End-frame flush (bad).
        logger.info(
            "TURN_LATENCY | vad+turn=%sms | llm_ttfb=%sms | llm_stream=%sms | "
            "tts_from_llm_start=%sms | tts_lag_after_llm_end=%sms | bot_start=%sms | user→bot=%sms",
            self._ms(self.t_vad_stop, self.t_user_stop),
            self._ms(self.t_user_stop, self._shared.t_llm_start),
            self._ms(self._shared.t_llm_start, self._shared.t_llm_end),
            self._ms(self._shared.t_llm_start, self.t_tts_first),
            self._ms(self._shared.t_llm_end, self.t_tts_first),
            self._ms(self.t_tts_first, self.t_bot_start),
            self._ms(self.t_vad_stop, self.t_bot_start),
        )

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        now = time.perf_counter()

        if isinstance(frame, VADUserStoppedSpeakingFrame):
            self._reset()
            self.t_vad_stop = now
        elif isinstance(frame, UserStoppedSpeakingFrame):
            self.t_user_stop = now
        elif isinstance(frame, TTSAudioRawFrame) and self.t_tts_first is None:
            self.t_tts_first = now
        elif isinstance(frame, BotStartedSpeakingFrame) and self.t_user_stop:
            self.t_bot_start = now
            self._maybe_log()

        await self.push_frame(frame, direction)
