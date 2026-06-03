"""
RMSAudioGate — silence-substitution gate on raw input audio.

Sits between transport.input() and STT. Computes per-frame RMS dBFS; if below
RMS_FLOOR_DBFS, replaces the frame's audio bytes with zeros of the same length
(preserves timing so streaming-STT sessions don't see gaps). Real near-field
caller speech passes through unchanged.

Once the downstream user-aggregator has decided a real user turn started
(UserStartedSpeakingFrame), the gate stops second-guessing for the rest of
that turn — sub-floor frames inside a confirmed turn pass through unchanged.
This prevents inter-syllable / fricative dips from being chopped out and
starving STT mid-utterance. The gate re-arms on UserStoppedSpeakingFrame.

Purpose: prevent STT from transcribing background / far-field audio that the
VAD-side RMS gate already decided to ignore. Without this, STT still produces
phantom transcripts upstream of the aggregator and they leak into Redis.
"""
from __future__ import annotations

import logging
import math

import numpy as np
from pipecat.frames.frames import (
    Frame,
    InputAudioRawFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from voicebot.config import RMS_FLOOR_DBFS

logger = logging.getLogger(__name__)


class RMSAudioGate(FrameProcessor):
    def __init__(self, log_every_n: int = 10):
        super().__init__()
        self._log_every_n = log_every_n
        self._gated_count = 0
        self._passed_count = 0
        self._in_user_turn = False

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)

        if isinstance(frame, UserStartedSpeakingFrame):
            self._in_user_turn = True
            # logger.info("rms_audio_gate | turn=open (gate bypassed until turn ends)")
        elif isinstance(frame, UserStoppedSpeakingFrame):
            self._in_user_turn = False
            # logger.info("rms_audio_gate | turn=closed (gate re-armed)")

        if (
            isinstance(frame, InputAudioRawFrame)
            and frame.audio
            and not self._in_user_turn
        ):
            audio_i16 = np.frombuffer(frame.audio, np.int16)
            if audio_i16.size:
                rms = float(np.sqrt(np.mean(audio_i16.astype(np.float32) ** 2))) / 32768.0
                rms_dbfs = 20.0 * math.log10(rms + 1e-9)
                if rms_dbfs < RMS_FLOOR_DBFS:
                    # Substitute very-low-amplitude dither (~-80 dBFS) instead
                    # of pure zeros. Some streaming STTs (Sarvam) degrade when
                    # fed long stretches of perfect-zero PCM — they treat it
                    # as a dead stream and stop responding to subsequent onset.
                    # A few-count int16 dither is inaudible but keeps the
                    # vendor's streaming endpointer healthy.
                    # dither = np.random.randint(
                    #     -3, 4, size=audio_i16.size, dtype=np.int16
                    # )
                    dither = np.random.randint(
                        -1,1, size=audio_i16.size, dtype=np.int16
                    )

                    frame.audio = dither.tobytes()
                    self._gated_count += 1
                    # if self._gated_count % self._log_every_n == 0:
                    #     logger.info(
                    #         "rms_audio_gate | muted=%d | passed=%d | last_rms_dbfs=%.1f | floor=%.1f",
                    #         self._gated_count, self._passed_count, rms_dbfs, RMS_FLOOR_DBFS,
                    #     )
                else:
                    self._passed_count += 1

        await self.push_frame(frame, direction)
