"""
FillerInjectorProcessor — emits a short language-matched TTS utterance shortly
after VAD detects user silence, masking the smart-turn wait + STT + LLM TTFB.

Timing:
  VAD-stop fires ~150ms after the user actually goes silent
  (VAD_MIN_SILENCE_MS). Firing the filler instantly on VAD-stop risks
  stepping on mid-utterance pauses (caller takes a breath, VAD stops, caller
  resumes). Instead, schedule the filler FILLER_DELAY_MS (~900ms) after
  VAD-stop and cancel if:
    - UserStartedSpeakingFrame arrives (caller resumed — false stop)
    - BotStartedSpeakingFrame arrives (LLM TTFB beat the delay — no need)
  By ~900ms we're confident the user genuinely stopped, but still ahead of
  the typical LLM+TTS latency.

Filler selection is state-aware: pick_filler is keyed on
`state.current_stage` so the phrase fits the conversational context (e.g.
empathetic during `persuade`, note-taking during `payment_intent`).

Placement (see pipeline.py): immediately AFTER context_aggr.user() and
BEFORE StageOverlayProcessor / llm. TTS is downstream, so we push
TTSSpeakFrame downstream.

The filler does NOT pass through the LLM and is NOT appended to LLMContext —
it goes straight to TTS. Pipecat's TTS service queues TTSSpeakFrame and the
subsequent LLM TextFrames in order, so the filler plays first and the real
response follows.

Single-fire-per-cycle: armed by UserStartedSpeakingFrame, disarmed once the
delayed task either fires or is cancelled.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    Frame,
    TTSSpeakFrame,
    UserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from voicebot.config import FILLER_DELAY_MS
from voicebot.prompts.fillers import pick_filler
from voicebot.state.language_state import LanguageState

logger = logging.getLogger(__name__)


class FillerInjectorProcessor(FrameProcessor):
    """Schedules a stage-aware prefix-filler TTSSpeakFrame ~FILLER_DELAY_MS
    after VADUserStoppedSpeakingFrame, cancellable on user resume or bot
    speech start."""

    def __init__(self, state: LanguageState):
        super().__init__()
        self._state = state
        self._armed: bool = False
        self._pending: Optional[asyncio.Task] = None

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)

        if isinstance(frame, UserStartedSpeakingFrame):
            # Caller resumed — cancel any pending filler and re-arm for the
            # next end-of-turn.
            self._cancel_pending("user_resumed")
            self._armed = True

        elif isinstance(frame, VADUserStoppedSpeakingFrame):
            if self._armed and not self._state.greeting_active:
                self._armed = False
                self._cancel_pending("rescheduled")
                self._pending = asyncio.create_task(self._fire_after_delay())

        elif isinstance(frame, BotStartedSpeakingFrame):
            # LLM beat the filler delay — skip the filler entirely.
            self._cancel_pending("bot_started")

        await self.push_frame(frame, direction)

    def _cancel_pending(self, reason: str) -> None:
        if self._pending and not self._pending.done():
            self._pending.cancel()
            logger.debug("filler_cancelled | reason=%s", reason)
        self._pending = None

    async def _fire_after_delay(self) -> None:
        try:
            await asyncio.sleep(FILLER_DELAY_MS / 1000.0)
        except asyncio.CancelledError:
            return

        text = pick_filler(
            language=self._state.current_language,
            stage=self._state.current_stage,
            end_of_call=self._state.end_after_speech,
        )
        if not text:
            return

        logger.info(
            "filler_injected | lang=%s | stage=%s | delay_ms=%d | text=%r",
            self._state.current_language,
            self._state.current_stage,
            FILLER_DELAY_MS,
            text,
        )
        await self.push_frame(TTSSpeakFrame(text=text), FrameDirection.DOWNSTREAM)
