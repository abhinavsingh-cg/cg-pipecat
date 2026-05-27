"""
SarvamTurnAnalyzerUserTurnStopStrategy — TurnAnalyzer subclass for Sarvam STT.

Problem: TurnAnalyzerUserTurnStopStrategy waits for TranscriptionFrame.finalized=True
before firing the turn-stop. Sarvam never sets finalized=True, so even when
Smart Turn predicts COMPLETE with high confidence AND the transcript has already
arrived, the strategy still waits for its internal _timeout_handler
(max(0, stt_p99 - VAD_stop_secs), typically 300-400ms) before firing.

Fix: treat every Sarvam TranscriptionFrame as if finalized=True. The
parent's _maybe_trigger_user_turn_stopped() will then fire as soon as
both `_text` is non-empty and `_turn_complete` is True (set by Smart Turn).

Saves ~300-400ms per turn versus the unmodified parent class when running
TURN_DETECTION_MODE=smart_turn (or smart_turn_early) with STT_PRIMARY=sarvam.
"""
from __future__ import annotations

import logging

from pipecat.frames.frames import TranscriptionFrame
from pipecat.turns.user_stop.turn_analyzer_user_turn_stop_strategy import (
    TurnAnalyzerUserTurnStopStrategy,
)

logger = logging.getLogger(__name__)


class SarvamTurnAnalyzerUserTurnStopStrategy(TurnAnalyzerUserTurnStopStrategy):
    """TurnAnalyzer strategy that treats every transcript as finalized,
    since Sarvam never sets TranscriptionFrame.finalized=True."""

    async def _handle_transcription(self, frame: TranscriptionFrame) -> None:
        # Force finalized=True so the parent's fast-path fires as soon as
        # Smart Turn says COMPLETE. Mutating the frame in place is safe —
        # the strategy is the only consumer that cares about this flag.
        if not frame.finalized:
            frame.finalized = True
            logger.info("sarvam_turn_analyzer | forced finalized=True")
        await super()._handle_transcription(frame)
