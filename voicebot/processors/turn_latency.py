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
    TranscriptionFrame,
    TTSAudioRawFrame,
    UserStoppedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

logger = logging.getLogger("voicebot.turn_latency")


class TurnLatencyTracker(FrameProcessor):
    def __init__(self) -> None:
        super().__init__()
        self._reset()

    def _reset(self) -> None:
        self.t_vad_stop = None
        self.t_user_stop = None
        self.t_transcript = None
        self.t_llm_start = None
        self.t_llm_end = None
        self.t_tts_first = None
        self.t_bot_start = None

    @staticmethod
    def _ms(a, b) -> str:
        if a is None or b is None:
            return "—"
        return f"{(b - a) * 1000:.0f}"

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        now = time.perf_counter()

        if isinstance(frame, VADUserStoppedSpeakingFrame):
            self._reset()
            self.t_vad_stop = now
        elif isinstance(frame, UserStoppedSpeakingFrame):
            self.t_user_stop = now
        elif isinstance(frame, TranscriptionFrame) and self.t_user_stop:
            self.t_transcript = now
        elif isinstance(frame, LLMFullResponseStartFrame):
            self.t_llm_start = now
        elif isinstance(frame, LLMFullResponseEndFrame):
            self.t_llm_end = now
        elif isinstance(frame, TTSAudioRawFrame) and self.t_tts_first is None:
            self.t_tts_first = now
        elif isinstance(frame, BotStartedSpeakingFrame) and self.t_user_stop:
            self.t_bot_start = now
            total = self._ms(self.t_user_stop, self.t_tts_first)
            logger.info(
                "TURN_LATENCY | vad+turn=%sms | stt=%sms | llm_ttfb=%sms | "
                "llm_total=%sms | tts_ttfb=%sms | bot_start=%sms | TOTAL=%sms",
                self._ms(self.t_vad_stop, self.t_user_stop),
                self._ms(self.t_user_stop, self.t_transcript),
                self._ms(self.t_transcript, self.t_llm_start),
                self._ms(self.t_llm_start, self.t_llm_end),
                self._ms(self.t_llm_start, self.t_tts_first),
                self._ms(self.t_tts_first, self.t_bot_start),
                total,
            )

        await self.push_frame(frame, direction)
