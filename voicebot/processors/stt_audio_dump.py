"""
STTAudioDump — debug tap that writes per-turn WAV files of the audio flowing
into STT (post-RMS-gate). Each user turn (UserStartedSpeakingFrame →
UserStoppedSpeakingFrame) gets its own file so you can pair playback against
the transcript log.

Includes ~400ms of pre-roll audio captured in a rolling buffer so you can hear
the moment before Silero/RMS decided the turn started — useful for diagnosing
clipped onsets.

Enable by setting STT_AUDIO_DUMP_PATH=/tmp/stt_dumps (a *directory*) in .env.
Files are written as: turn_<N>_<HHMMSS>.wav

The processor logs one line per closed turn so you can grep by timestamp to
match a file against its `user_transcript` log line.
"""
from __future__ import annotations

import logging
import os
import time
import wave
from collections import deque
from typing import Optional

from pipecat.frames.frames import (
    EndFrame,
    Frame,
    InputAudioRawFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

logger = logging.getLogger(__name__)

PRE_ROLL_FRAMES = 20  # 20 frames × 20ms = 400ms pre-roll


class STTAudioDump(FrameProcessor):
    def __init__(self, sample_rate: int = 8000, dir_path: Optional[str] = None):
        super().__init__()
        self._dir = dir_path or os.getenv("STT_AUDIO_DUMP_PATH") or ""
        self._sample_rate = sample_rate
        self._turn_idx = 0
        self._wf: Optional[wave.Wave_write] = None
        self._current_path: Optional[str] = None
        self._bytes_written = 0
        self._pre_roll: deque[bytes] = deque(maxlen=PRE_ROLL_FRAMES)

        if self._dir:
            os.makedirs(self._dir, exist_ok=True)
            logger.info("stt_audio_dump | dumping per-turn WAVs to %s/", self._dir)
        else:
            logger.info("stt_audio_dump | disabled (set STT_AUDIO_DUMP_PATH to a directory)")

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)

        if self._dir:
            if isinstance(frame, UserStartedSpeakingFrame):
                # Pipecat re-emits UserStartedSpeakingFrame after an
                # interruption cascade. Ignore duplicates so a single
                # utterance produces a single file.
                if self._wf is None:
                    self._open_turn()
                else:
                    logger.debug("stt_audio_dump | duplicate UserStartedSpeakingFrame ignored")
            elif isinstance(frame, UserStoppedSpeakingFrame):
                self._close_turn()
            elif isinstance(frame, InputAudioRawFrame) and frame.audio:
                if self._wf is not None:
                    self._write(frame.audio)
                else:
                    self._pre_roll.append(frame.audio)
            elif isinstance(frame, EndFrame):
                self._close_turn()

        await self.push_frame(frame, direction)

    def _open_turn(self) -> None:
        self._close_turn()  # safety: close any leftover open file
        self._turn_idx += 1
        ts = time.strftime("%H%M%S")
        self._current_path = os.path.join(self._dir, f"turn_{self._turn_idx:03d}_{ts}.wav")
        try:
            self._wf = wave.open(self._current_path, "wb")
            self._wf.setnchannels(1)
            self._wf.setsampwidth(2)
            self._wf.setframerate(self._sample_rate)
            self._bytes_written = 0
            for chunk in self._pre_roll:
                self._write(chunk)
            logger.info(
                "stt_audio_dump | turn=%d open | path=%s | pre_roll_frames=%d",
                self._turn_idx, self._current_path, len(self._pre_roll),
            )
        except Exception as exc:
            logger.warning("stt_audio_dump | open failed: %s", exc)
            self._wf = None

    def _write(self, audio: bytes) -> None:
        if self._wf is None:
            return
        try:
            self._wf.writeframesraw(audio)
            self._bytes_written += len(audio)
        except Exception as exc:
            logger.warning("stt_audio_dump | write failed: %s", exc)

    def _close_turn(self) -> None:
        if self._wf is None:
            return
        try:
            self._wf.close()
            secs = self._bytes_written / 2 / self._sample_rate
            logger.info(
                "stt_audio_dump | turn=%d closed | path=%s | duration=%.2fs",
                self._turn_idx, self._current_path, secs,
            )
        except Exception as exc:
            logger.warning("stt_audio_dump | close failed: %s", exc)
        finally:
            self._wf = None
            self._current_path = None
            self._bytes_written = 0

    def __del__(self):
        self._close_turn()
