"""
SarvamVADUserTurnStopStrategy — SpeechTimeout subclass for Sarvam STT.

Sarvam never sets TranscriptionFrame.finalized=True, so the parent class
SpeechTimeoutUserTurnStopStrategy waits for the full stt_timeout P99 (~800ms)
before setting _stt_wait_done=True, even though the transcript has already
arrived. This turns a ~250ms STT into a ~880ms turn-detection delay.

Fix: on VADUserStoppedSpeakingFrame, immediately cancel the stt_timeout_task
and mark _stt_wait_done=True. VAD stop is our finalization signal for Sarvam —
we trust that the transcript will arrive shortly after (it does, ~250ms later).
When it arrives, both gates (_user_speech_wait_done + _stt_wait_done) are
already open, so the turn fires immediately.

Sarvam's own END_SPEECH UserStoppedSpeakingFrame is suppressed in stt/factory.py
since it's redundant — VAD already handles detection and the extra frame just
polluted the turn controller state.
"""
from __future__ import annotations

import logging

from pipecat.frames.frames import VADUserStoppedSpeakingFrame
from pipecat.turns.user_stop.speech_timeout_user_turn_stop_strategy import (
    SpeechTimeoutUserTurnStopStrategy,
)

logger = logging.getLogger(__name__)


class SarvamVADUserTurnStopStrategy(SpeechTimeoutUserTurnStopStrategy):
    """SpeechTimeout strategy that cancels the stt_timeout safety-net on VAD
    stop, since Sarvam never sets TranscriptionFrame.finalized=True."""

    async def _handle_vad_user_stopped_speaking(
        self, frame: VADUserStoppedSpeakingFrame
    ) -> None:
        await super()._handle_vad_user_stopped_speaking(frame)
        # Sarvam transcripts arrive ~250ms after VAD stop and are never
        # finalized=True. Cancel the ~800ms stt_timeout_task immediately so
        # the turn can fire the moment the transcript lands.
        if not self._stt_wait_done:
            self._stt_wait_done = True
            if self._stt_timeout_task:
                await self.task_manager.cancel_task(self._stt_timeout_task)
                self._stt_timeout_task = None
            logger.info("sarvam_vad_stop | stt_wait_done=True on VAD stop")
