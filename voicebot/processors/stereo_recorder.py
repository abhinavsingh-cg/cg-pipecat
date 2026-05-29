"""
StereoCallRecorder — debug recording that captures the borrower on the LEFT
channel and the bot on the RIGHT channel, written to a stereo WAV on call end.

Enable with CALL_RECORDING_ENABLED=true. Output: {CALL_RECORDING_DIR}/{call_id}.wav.

Wiring (see pipeline.py): one shared buffer, two taps —
  - UserAudioTap: just after transport.input(), captures InputAudioRawFrame (left)
  - BotAudioTap:  just after TTS, captures OutputAudioRawFrame (right)
Both finalize on EndFrame (finalize is idempotent).
"""
from __future__ import annotations

import logging
import os
import wave

from pipecat.frames.frames import (
    EndFrame,
    Frame,
    InputAudioRawFrame,
    OutputAudioRawFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from voicebot.config import CALL_RECORDING_DIR

logger = logging.getLogger(__name__)


class _StereoBuffer:
    """Shared accumulator. left/right hold int16 mono PCM (2 bytes per sample)."""

    def __init__(self, call_id: str, *, sample_rate: int = 8000, output_dir: str = CALL_RECORDING_DIR):
        self.call_id = call_id
        self.sample_rate = sample_rate
        self.output_dir = output_dir
        self.left = bytearray()   # borrower
        self.right = bytearray()  # bot
        self._finalized = False

    def write_user(self, pcm: bytes) -> None:
        self.left.extend(pcm)

    def write_bot(self, pcm: bytes) -> None:
        self.right.extend(pcm)

    def finalize(self) -> str | None:
        if self._finalized:
            return None
        self._finalized = True

        if not self.left and not self.right:
            logger.info("stereo_recorder | nothing recorded for %s", self.call_id)
            return None

        # Pad the shorter channel with silence so both have the same length.
        max_len = max(len(self.left), len(self.right))
        max_len -= max_len % 2  # keep whole int16 samples
        self.left.extend(b"\x00" * (max_len - len(self.left)))
        self.right.extend(b"\x00" * (max_len - len(self.right)))

        # Interleave: [L0 L1 R0 R1] [L2 L3 R2 R3] ... (2 bytes per sample).
        stereo = bytearray(max_len * 2)
        for i in range(0, max_len, 2):
            stereo[i * 2 : i * 2 + 2] = self.left[i : i + 2]
            stereo[i * 2 + 2 : i * 2 + 4] = self.right[i : i + 2]

        os.makedirs(self.output_dir, exist_ok=True)
        path = os.path.join(self.output_dir, f"{self.call_id}.wav")
        with wave.open(path, "wb") as wf:
            wf.setnchannels(2)
            wf.setsampwidth(2)
            wf.setframerate(self.sample_rate)
            wf.writeframes(bytes(stereo))

        logger.info(
            "stereo_recorder | saved %s | user_bytes=%d | bot_bytes=%d",
            path, len(self.left), len(self.right),
        )
        return path


class UserAudioTap(FrameProcessor):
    """Captures InputAudioRawFrame into the left channel."""

    def __init__(self, buf: _StereoBuffer):
        super().__init__()
        self._buf = buf

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if isinstance(frame, InputAudioRawFrame):
            self._buf.write_user(frame.audio)
        elif isinstance(frame, EndFrame):
            self._buf.finalize()
        await self.push_frame(frame, direction)


class BotAudioTap(FrameProcessor):
    """Captures OutputAudioRawFrame into the right channel."""

    def __init__(self, buf: _StereoBuffer):
        super().__init__()
        self._buf = buf

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if isinstance(frame, OutputAudioRawFrame):
            self._buf.write_bot(frame.audio)
        elif isinstance(frame, EndFrame):
            self._buf.finalize()
        await self.push_frame(frame, direction)


class StereoCallRecorder:
    """Convenience wrapper: owns the shared buffer and exposes the two taps."""

    def __init__(self, call_id: str, *, sample_rate: int = 8000):
        self._buf = _StereoBuffer(call_id, sample_rate=sample_rate)
        self.user_tap = UserAudioTap(self._buf)
        self.bot_tap = BotAudioTap(self._buf)

    def finalize(self) -> str | None:
        return self._buf.finalize()
