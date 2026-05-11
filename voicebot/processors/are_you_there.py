"""
AreYouThereWatchdog — idle-prompt strikes (ported from rtp_processor.py:2572+).

BEHAVIOR:
  • After ARE_YOU_THERE_TIMEOUT_S seconds of silence (no user speech, no bot
    speech), pushes a TTSSpeakFrame with a localized "are you there?" prompt.
  • After ARE_YOU_THERE_MAX_STRIKES unanswered prompts, pushes EndFrame
    (which causes Pipecat to cleanly shut down the pipeline / hang up).
  • Timer resets whenever:
      - User starts speaking (UserStartedSpeakingFrame)
      - Bot starts speaking (BotStartedSpeakingFrame) — timer paused
      - Bot stops speaking (BotStoppedSpeakingFrame) — timer resumed

CUSTOMIZE:
  • Change timeouts: ARE_YOU_THERE_TIMEOUT_S / ARE_YOU_THERE_MAX_STRIKES in .env.
  • Add / change prompts: edit ARE_YOU_THERE_TEXT below.
  • Change hangup behavior: replace EndFrame with a custom frame or add a
    Redis cleanup step before pushing EndFrame.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    EndFrame,
    Frame,
    StartFrame,
    TTSSpeakFrame,
    UserStartedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from voicebot.config import ARE_YOU_THERE_MAX_STRIKES, ARE_YOU_THERE_TIMEOUT_S
from voicebot.prompts.runtime_messages import (
    ARE_YOU_THERE_TEXT,
    REPEAT_MESSAGE,
    localized_runtime_message,
)
from voicebot.state.language_state import LanguageState

logger = logging.getLogger(__name__)


class AreYouThereWatchdog(FrameProcessor):
    def __init__(self, state: LanguageState):
        super().__init__()
        self._state = state
        self._task: Optional[asyncio.Task] = None
        self._bot_speaking = False

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)

        if isinstance(frame, StartFrame):
            # Pipeline started — kick off the initial idle timer.
            self._reset_timer()
        elif isinstance(frame, UserStartedSpeakingFrame):
            # User is active — reset strike counter and restart idle timer.
            self._state.are_you_there_count = 0
            self._reset_timer()
        elif isinstance(frame, BotStartedSpeakingFrame):
            # Don't fire "are you there?" while the bot is talking.
            self._bot_speaking = True
            self._cancel_timer()
        elif isinstance(frame, BotStoppedSpeakingFrame):
            # Bot finished — restart the idle clock.
            self._bot_speaking = False
            self._reset_timer()

        await self.push_frame(frame, direction)

    def _reset_timer(self) -> None:
        self._cancel_timer()
        self._task = asyncio.create_task(self._fire())

    def _cancel_timer(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
        self._task = None

    async def _fire(self) -> None:
        try:
            await asyncio.sleep(ARE_YOU_THERE_TIMEOUT_S)
        except asyncio.CancelledError:
            return
        if self._bot_speaking:
            return
        self._state.are_you_there_count += 1
        logger.info("are_you_there strike %d/%d",
                    self._state.are_you_there_count, ARE_YOU_THERE_MAX_STRIKES)
        if self._state.are_you_there_count >= ARE_YOU_THERE_MAX_STRIKES:
            # Max strikes reached — end the call.
            await self.push_frame(EndFrame(), FrameDirection.DOWNSTREAM)
            return
        language_key = self._state.last_detected_language or self._state.current_language
        messages = REPEAT_MESSAGE if self._state.are_you_there_count == 1 else ARE_YOU_THERE_TEXT
        text = localized_runtime_message(messages, language_key)
        # TTSSpeakFrame bypasses the LLM and goes directly to the TTS service.
        # This processor sits AFTER the TTS service in the pipeline, so the
        # frame must travel UPSTREAM to reach it. Pushing downstream would
        # send it toward transport.output(), which would never synthesize it.
        await self.push_frame(TTSSpeakFrame(text=text), FrameDirection.UPSTREAM)
        # Restart so we keep checking until the user replies.
        self._reset_timer()
