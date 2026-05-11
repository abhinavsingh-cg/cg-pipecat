"""
GreetingGate — makes the first bot turn (the deterministic greeting injected
via TTSSpeakFrame at call start) uninterruptible, while still letting the
bot LISTEN throughout: STT, VAD, and the user aggregator are all upstream
and keep running normally. Only the InterruptionFrame broadcast that would
cancel TTS is suppressed.

Two cooperating processors share a single boolean flag on LanguageState:

  GreetingGate (placed BETWEEN context_aggr.user() and tts):
    Drops InterruptionFrame while state.greeting_active is True. This stops
    barge-in from cancelling the in-flight TTS playback of the greeting while
    still letting user-start events flow through the pipeline.

  GreetingDoneFlag (placed AFTER tts):
    Watches for the first TTSStoppedFrame / BotStoppedSpeakingFrame and flips
    the flag off, after which normal barge-in resumes.

LanguageState must initialise greeting_active=True.
"""
from __future__ import annotations

import logging

from pipecat.frames.frames import (
    BotStoppedSpeakingFrame,
    Frame,
    InterruptionFrame,
    TTSStoppedFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from voicebot.state.language_state import LanguageState

logger = logging.getLogger(__name__)


class GreetingGate(FrameProcessor):
    def __init__(self, state: LanguageState):
        super().__init__()
        self._state = state

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if self._state.greeting_active and isinstance(frame, InterruptionFrame):
            return  # swallow — TTS keeps playing the greeting
        await self.push_frame(frame, direction)


class GreetingDoneFlag(FrameProcessor):
    def __init__(self, state: LanguageState):
        super().__init__()
        self._state = state

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if self._state.greeting_active and isinstance(
            frame,
            (TTSStoppedFrame, BotStoppedSpeakingFrame),
        ):
            self._state.greeting_active = False
            logger.info("greeting gate disabled")
        await self.push_frame(frame, direction)
