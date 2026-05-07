"""
EarlyBargeInConcatProcessor — preserves the early-barge-in concat behavior
from credgenics-voice-bot-library (rtp_processor.py:1015-1019, 2690-2703,
1304-1319).

If the user interrupts the bot within EARLY_BARGE_IN_WINDOW_S of the bot
starting to speak AND the LLM had begun emitting tokens:
  1. Stash the previous user transcript on state.pending_concat_text.
  2. Trim the last user + assistant pair from Redis.

On the next final TranscriptionFrame, prepend the stash to the new text so
the LLM sees one merged user turn.
"""
from __future__ import annotations

import logging
import time
from typing import Optional

from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    Frame,
    InterruptionFrame,
    LLMFullResponseStartFrame,
    TranscriptionFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from voicebot.config import EARLY_BARGE_IN_WINDOW_S
from voicebot.state.language_state import LanguageState
from voicebot.state.redis_memory import RedisMemory

logger = logging.getLogger(__name__)


class EarlyBargeInConcatProcessor(FrameProcessor):
    def __init__(self, state: LanguageState, memory: Optional[RedisMemory] = None):
        super().__init__()
        self._state = state
        self._memory = memory

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)

        if isinstance(frame, BotStartedSpeakingFrame):
            self._state.bot_speech_start_ts = time.monotonic()
            self._state.llm_output_inserted = False

        elif isinstance(frame, BotStoppedSpeakingFrame):
            self._state.bot_speech_start_ts = None

        elif isinstance(frame, LLMFullResponseStartFrame):
            self._state.llm_output_inserted = True

        elif isinstance(frame, InterruptionFrame):
            await self._maybe_stash()

        elif isinstance(frame, TranscriptionFrame) and frame.text:
            self._merge_pending(frame)

        await self.push_frame(frame, direction)

    async def _maybe_stash(self) -> None:
        ts = self._state.bot_speech_start_ts
        if ts is None:
            return
        elapsed = time.monotonic() - ts
        if elapsed > EARLY_BARGE_IN_WINDOW_S:
            return
        if not self._state.llm_output_inserted:
            return
        if not self._state.last_user_transcription:
            return
        self._state.pending_concat_text = self._state.last_user_transcription
        logger.info(
            "early_barge_in: stashed prior transcript (elapsed=%.3fs): %r",
            elapsed, self._state.pending_concat_text,
        )
        if self._memory is not None:
            try:
                await self._memory.trim_last(2)
            except Exception as exc:
                logger.warning("early_barge_in: redis trim_last(2) failed: %s", exc)

    def _merge_pending(self, frame: TranscriptionFrame) -> None:
        merged_in = self._state.pending_concat_text
        if merged_in:
            frame.text = f"{merged_in} {frame.text}"
            self._state.pending_concat_text = None
            logger.info("early_barge_in: merged transcript: %r", frame.text)
        # Always remember the most recent user transcript for the next round.
        self._state.last_user_transcription = frame.text
