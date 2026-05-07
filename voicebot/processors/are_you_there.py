"""
AreYouThereWatchdog — idle-prompt strikes.

After ARE_YOU_THERE_TIMEOUT_S of silence, emits a TTSSpeakFrame with the
language-appropriate "are you there?" prompt. After ARE_YOU_THERE_MAX_STRIKES
strikes, ends the pipeline.

Ports rtp_processor.py:2572+ behavior.
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
from voicebot.state.language_state import LanguageState

logger = logging.getLogger(__name__)

# Localized "are you there?" prompts (port from cg_voicebot/config.py:135-147).
ARE_YOU_THERE_TEXT = {
    "english": "Hello, are you there?",
    "hindi": "क्या आप मेरी बात सुन पा रहे हैं?",
    "telugu": "మీరు నా మాట వినగలరా?",
    "kannada": "ನೀವು ನನ್ನ ಮಾತು ಕೇಳುತ್ತೀರಾ?",
    "tamil": "நீங்கள் என் பேச்சை கேட்கிறீர்களா?",
    "bengali": "আপনি কি আমার কথা শুনতে পাচ্ছেন?",
    "malayalam": "നിങ്ങൾ എന്റെ ശബ്ദം കേൾക്കുന്നുണ്ടോ?",
    "marathi": "तुम्ही माझं बोलणं ऐकू शकता का?",
    "gujarati": "શું તમે મારી વાત સાંભળી શકો છો?",
    "punjabi": "ਕੀ ਤੁਸੀਂ ਮੇਰੀ ਗੱਲ ਸੁਣ ਸਕਦੇ ਹੋ?",
    "urdu": "کیا آپ میری بات سن رہے ہیں؟",
}


class AreYouThereWatchdog(FrameProcessor):
    def __init__(self, state: LanguageState):
        super().__init__()
        self._state = state
        self._task: Optional[asyncio.Task] = None
        self._bot_speaking = False

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)

        if isinstance(frame, StartFrame):
            self._reset_timer()
        elif isinstance(frame, UserStartedSpeakingFrame):
            self._state.are_you_there_count = 0
            self._reset_timer()
        elif isinstance(frame, BotStartedSpeakingFrame):
            self._bot_speaking = True
            self._cancel_timer()
        elif isinstance(frame, BotStoppedSpeakingFrame):
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
            await self.push_frame(EndFrame(), FrameDirection.DOWNSTREAM)
            return
        text = ARE_YOU_THERE_TEXT.get(
            self._state.current_language, ARE_YOU_THERE_TEXT["english"]
        )
        await self.push_frame(TTSSpeakFrame(text=text), FrameDirection.DOWNSTREAM)
        # Restart the timer so we keep polling until the user replies.
        self._reset_timer()
