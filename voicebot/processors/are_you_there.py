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
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    StartFrame,
    TTSSpeakFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
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

# Localized "are you there?" prompts (ported from cg_voicebot/config.py:135-147).
# CUSTOMIZE: add a language key here that matches keys in SUPPORTED_LNG_SUFFIX.
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
    "en": "Hello, are you there?",
    "hi": "क्या आप मेरी बात सुन पा रहे हैं?",
    "te": "మీరు నా మాట వినగలరా?",
    "kn": "ನೀವು ನನ್ನ ಮಾತು ಕೇಳುತ್ತೀರಾ?",
    "ta": "நீங்கள் என் பேச்சை கேட்கிறீர்களா?",
    "bn": "আপনি কি আমার কথা শুনতে পাচ্ছেন?",
    "ml": "നിങ്ങൾ എന്റെ ശബ്ദം കേൾക്കുന്നുണ്ടോ?",
    "mr": "तुम्ही माझं बोलणं ऐकू शकता का?",
    "gu": "શું તમે મારી વાત સાંભળી શકો છો?",
    "pu": "ਕੀ ਤੁਸੀਂ ਮੇਰੀ ਗੱਲ ਸੁਣ ਸਕਦੇ ਹੋ?",
    "pa": "ਕੀ ਤੁਸੀਂ ਮੇਰੀ ਗੱਲ ਸੁਣ ਸਕਦੇ ਹੋ?",
    "ur": "کیا آپ میری بات سن رہے ہیں؟",

}

REPEAT_MESSAGE = {
    "english": "Sorry, could you please repeat that?",
    "hindi": "माफ कीजिए, क्या आप एक बार फिर से बोल सकते हैं?",
    "telugu": "క్షమించండి, దయచేసి దాన్ని మళ్ళీ చెప్పగలరా?",
    "kannada": "ಕ್ಷಮಿಸಿ, ದಯವಿಟ್ಟು ಅದನ್ನು ಪುನರಾವರ್ತಿಸಬಹುದೇ?",
    "tamil": "மன்னிக்கவும், தயவுசெய்து அதை மீண்டும் கூற முடியுமா?",
    "marathi": "माफ करा, कृपया ते पुन्हा सांगू शकाल का?",
    "gujarati": "માફ કરશો, કૃપા કરીને તેને ફરીથી કહી શકશો?",
    "malayalam": "ക്ഷമിക്കണം, ദയവായി അത് വീണ്ടും പറയാമോ?",
    "bengali": "দুঃখিত, আপনি কি আবার বলতে পারবেন?",
    "panjabi": "ਮਾਫ ਕਰਨਾ, ਕੀ ਤੁਸੀਂ ਇਸਨੂੰ ਦੁਬਾਰਾ ਕਹਿ ਸਕਦੇ ਹੋ?",
    "punjabi": "ਮਾਫ ਕਰਨਾ, ਕੀ ਤੁਸੀਂ ਇਸਨੂੰ ਦੁਬਾਰਾ ਕਹਿ ਸਕਦੇ ਹੋ?",
}


class AreYouThereWatchdog(FrameProcessor):
    def __init__(self, state: LanguageState):
        super().__init__()
        self._state = state
        self._task: Optional[asyncio.Task] = None
        self._bot_speaking = False
        self._user_speaking = False
        self._waiting_for_llm = False

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)

        if isinstance(frame, StartFrame):
            # Pipeline started — kick off the initial idle timer.
            self._reset_timer()
        elif isinstance(frame, UserStartedSpeakingFrame):
            # User is actively talking — clear strikes and pause the idle timer.
            self._state.are_you_there_count = 0
            self._user_speaking = True
            self._cancel_timer()
        elif isinstance(frame, UserStoppedSpeakingFrame):
            # User turn ended — now we wait for the LLM, so keep the idle timer paused.
            self._user_speaking = False
            self._waiting_for_llm = True
            self._cancel_timer()
        elif isinstance(frame, LLMFullResponseStartFrame):
            # The bot is thinking / generating — don't let the idle watchdog fire.
            self._waiting_for_llm = True
            self._cancel_timer()
        elif isinstance(frame, LLMFullResponseEndFrame):
            # Generation finished; if TTS doesn't start immediately, restart the idle clock.
            self._waiting_for_llm = False
            if not self._bot_speaking and not self._user_speaking:
                self._reset_timer()
        elif isinstance(frame, BotStartedSpeakingFrame):
            # Don't fire "are you there?" while the bot is talking.
            self._bot_speaking = True
            self._waiting_for_llm = False
            self._cancel_timer()
        elif isinstance(frame, BotStoppedSpeakingFrame):
            # Bot finished — restart the idle clock.
            self._bot_speaking = False
            self._waiting_for_llm = False
            if not self._user_speaking:
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
        if self._bot_speaking or self._user_speaking or self._waiting_for_llm:
            return
        self._state.are_you_there_count += 1
        logger.info("are_you_there strike %d/%d",
                    self._state.are_you_there_count, ARE_YOU_THERE_MAX_STRIKES)
        if self._state.are_you_there_count >= ARE_YOU_THERE_MAX_STRIKES:
            # Max strikes reached — end the call.
            await self.push_frame(EndFrame(), FrameDirection.DOWNSTREAM)
            return
        text = self._prompt_text()
        # TTSSpeakFrame bypasses the LLM and goes directly to the TTS service.
        # This processor sits AFTER the TTS service in the pipeline, so the
        # frame must travel UPSTREAM to reach it. Pushing downstream would
        # send it toward transport.output(), which would never synthesize it.
        await self.push_frame(TTSSpeakFrame(text=text), FrameDirection.UPSTREAM)
        # Restart so we keep checking until the user replies.
        self._reset_timer()

    def _prompt_text(self) -> str:
        if self._state.are_you_there_count == 1:
            language = self._state.last_detected_language or self._state.current_language
            return REPEAT_MESSAGE.get(language, REPEAT_MESSAGE["english"])
        return ARE_YOU_THERE_TEXT.get(
            self._state.current_language,
            ARE_YOU_THERE_TEXT["english"],
        )
