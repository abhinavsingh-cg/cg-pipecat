"""
GreetingGate — makes the first bot turn (the deterministic greeting injected
via TTSSpeakFrame at call start) uninterruptible, while still letting the
bot LISTEN throughout: STT, VAD, and the user aggregator are all upstream
and keep running normally. Only the InterruptionFrame broadcast that would
cancel TTS is suppressed.

Two cooperating processors share a single boolean flag on LanguageState:

  GreetingGate (placed BETWEEN context_aggr.user() and tts):
    Drops InterruptionFrame / UserStartedSpeakingFrame while
    state.greeting_active is True. This stops barge-in from cancelling
    the in-flight TTS playback of the greeting.

  GreetingDoneFlag (placed AFTER transport.output()):
    Watches for the first BotStoppedSpeakingFrame and flips the flag off,
    after which normal barge-in resumes.

LanguageState must initialise greeting_active=True.
"""
from __future__ import annotations

from pipecat.frames.frames import (
    BotStoppedSpeakingFrame,
    Frame,
    InterruptionFrame,
    UserStartedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from voicebot.state.language_state import LanguageState


class GreetingGate(FrameProcessor):
    def __init__(self, state: LanguageState):
        super().__init__()
        self._state = state

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if self._state.greeting_active and isinstance(
            frame, (InterruptionFrame, UserStartedSpeakingFrame)
        ):
            return  # swallow — TTS keeps playing the greeting
        await self.push_frame(frame, direction)


class GreetingDoneFlag(FrameProcessor):
    def __init__(self, state: LanguageState):
        super().__init__()
        self._state = state

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if self._state.greeting_active and isinstance(frame, BotStoppedSpeakingFrame):
            self._state.greeting_active = False
        await self.push_frame(frame, direction)
