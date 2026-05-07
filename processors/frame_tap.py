"""
FrameTap — debug processor that logs every frame passing through.

Drop into the pipeline at any position to see what's flowing where.
Logs frame type + a 1-line summary; never mutates frames.
"""
from __future__ import annotations

import logging

from pipecat.frames.frames import (
    Frame,
    InputAudioRawFrame,
    OutputAudioRawFrame,
    TTSAudioRawFrame,
    TextFrame,
    TranscriptionFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

logger = logging.getLogger("voicebot.frames")

# Skip the high-volume audio frames in summary mode.
_NOISY = (InputAudioRawFrame, OutputAudioRawFrame, TTSAudioRawFrame)


class FrameTap(FrameProcessor):
    def __init__(self, label: str, *, include_audio: bool = False, log_every_audio: int = 50):
        super().__init__()
        self._label = label
        self._include_audio = include_audio
        self._every = log_every_audio
        self._audio_count = 0

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if isinstance(frame, _NOISY):
            self._audio_count += 1
            if self._include_audio and self._audio_count % self._every == 0:
                logger.info("[%s %s] %s #%d", self._label, direction.name,
                            type(frame).__name__, self._audio_count)
        else:
            extra = ""
            if isinstance(frame, TranscriptionFrame):
                extra = f" text={frame.text!r}"
            elif isinstance(frame, TextFrame):
                extra = f" text={frame.text!r}"
            logger.info("[%s %s] %s%s", self._label, direction.name,
                        type(frame).__name__, extra)
        await self.push_frame(frame, direction)
