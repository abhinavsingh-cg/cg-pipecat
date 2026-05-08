"""
EndCallTrigger — fires the hangup hook once the bot finishes speaking the
goodbye that contained the "| END |" marker.

The marker itself is detected and stripped upstream by
TextNormalizationProcessor, which sets state.end_after_speech = True. This
processor sits AFTER the TTS service so it observes BotStoppedSpeakingFrame,
which is emitted when audio playback of the response ends.

Flow:
  TextNormalizationProcessor sees "| END |" in streamed LLM text
    → strips it, sets state.end_after_speech = True
  TTS plays the cleaned goodbye
  TTS emits BotStoppedSpeakingFrame
    → EndCallTrigger sees it, calls end_call_action(), pushes EndFrame
"""
from __future__ import annotations

import logging
from typing import Awaitable, Callable, Optional, Union

from pipecat.frames.frames import BotStoppedSpeakingFrame, EndFrame, Frame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from voicebot.state.language_state import LanguageState

logger = logging.getLogger(__name__)


async def end_call_action() -> bool:
    """Proxy hangup hook. Wire real cleanup (SIP BYE, CRM update, …) here."""
    logger.info("end_call_action invoked")
    return True


HangupHook = Callable[[], Union[bool, Awaitable[bool]]]


class EndCallTrigger(FrameProcessor):
    def __init__(self, state: LanguageState, on_end: Optional[HangupHook] = None):
        super().__init__()
        self._state = state
        self._on_end = on_end or end_call_action
        self._fired = False

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)

        if (
            isinstance(frame, BotStoppedSpeakingFrame)
            and self._state.end_after_speech
            and not self._fired
        ):
            self._fired = True
            self._state.end_after_speech = False
            await self.push_frame(frame, direction)
            try:
                result = self._on_end()
                if hasattr(result, "__await__"):
                    result = await result  # type: ignore[assignment]
                logger.info("end_call hook returned %r", result)
            except Exception as exc:
                logger.warning("end_call hook raised: %s", exc)
            await self.push_frame(EndFrame(), FrameDirection.DOWNSTREAM)
            return

        await self.push_frame(frame, direction)
