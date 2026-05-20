"""
FillerInjectorProcessor — emits a short language-matched TTS utterance the
moment VAD detects user silence, masking the smart-turn wait + STT + LLM TTFB.

Why VADUserStoppedSpeakingFrame (not UserStoppedSpeakingFrame):
  VAD-stop fires ~150ms after the user actually goes silent (VAD_MIN_SILENCE_MS).
  The downstream UserStoppedSpeakingFrame only emits AFTER smart-turn audio
  collection + STT settling — often 1+ second later. Firing on VAD-stop puts
  the filler audio in the caller's ear almost immediately. If smart-turn later
  decides "not actually end of turn" (rare when prob ≥ 0.9), the existing
  barge-in path cancels in-flight TTS — no special handling needed.

Placement (see pipeline.py): immediately AFTER context_aggr.user() and
BEFORE StageOverlayProcessor / llm. TTS is downstream, so we push
TTSSpeakFrame downstream (unlike AreYouThereWatchdog which sits after TTS
and pushes upstream).

The filler does NOT pass through the LLM and is NOT appended to LLMContext —
it goes straight to TTS. Pipecat's TTS service queues TTSSpeakFrame and the
subsequent LLM TextFrames in order, so the filler plays first and the real
response follows.

Single-fire-per-cycle: armed by UserStartedSpeakingFrame, disarmed after
firing. Prevents duplicate VAD/user-stop echoes from firing twice.
"""
from __future__ import annotations

import logging

from pipecat.frames.frames import (
    Frame,
    TTSSpeakFrame,
    UserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from voicebot.prompts.fillers import pick_filler
from voicebot.state.language_state import LanguageState

logger = logging.getLogger(__name__)


class FillerInjectorProcessor(FrameProcessor):
    """Emits a prefix-filler TTSSpeakFrame on VADUserStoppedSpeakingFrame."""

    def __init__(self, state: LanguageState):
        super().__init__()
        self._state = state
        # Armed on UserStartedSpeakingFrame, disarmed once a filler ships, so
        # exactly one filler fires per user-speech cycle (no double-fire if
        # VAD chatters or smart-turn re-emits stops).
        self._armed: bool = False

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)

        if isinstance(frame, UserStartedSpeakingFrame):
            self._armed = True

        elif isinstance(frame, VADUserStoppedSpeakingFrame):
            if self._armed and not self._state.greeting_active:
                self._armed = False
                text = pick_filler(
                    language=self._state.current_language,
                    end_of_call=self._state.end_after_speech,
                )
                if text:
                    logger.info(
                        "filler_injected | lang=%s | text=%r",
                        self._state.current_language,
                        text,
                    )
                    await self.push_frame(
                        TTSSpeakFrame(text=text), FrameDirection.DOWNSTREAM
                    )

        await self.push_frame(frame, direction)
