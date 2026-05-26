"""
EarlyTranscriptionUserTurnStopStrategy — fire the LLM the moment a streaming
STT confirms end-of-utterance, even if VAD hasn't yet emitted
VADUserStoppedSpeakingFrame.

Extends SpeechTimeoutUserTurnStopStrategy so the fallback machinery (VAD stop
+ user_speech_timeout + stt_timeout safety net) stays intact for utterances
that don't satisfy the early-fire conditions (short "haan", no punctuation,
batch STT, low confidence, etc.).

Early-fire conditions (all must hold):
  1. A TranscriptionFrame is received (which is always a final result in
     practice — interims arrive as InterimTranscriptionFrame and never
     reach _handle_transcription). The frame.finalized field is NOT used,
     because Deepgram only stamps it when our send_finalize() request
     races ahead of Deepgram's own endpointing — unreliable.
  2. Text is non-empty
  3. Per-frame confidence >= EARLY_TRIGGER_MIN_CONF (extracted from
     frame.result if the vendor populates it; defaults to 1.0 when absent)
  4. Not already fired for this turn (one-shot per user-start cycle)

When all conditions hold, the strategy snapshots the gate state
(_user_speech_wait_done = _stt_wait_done = True, _vad_user_speaking = False),
cancels pending timer tasks, and fires trigger_user_turn_stopped() directly.

If the conditions don't hold, the parent class's behaviour (VAD stop +
user_speech_timeout) takes over unchanged.

Hindi note: `।` (U+0964 Devanagari danda) is included as sentence-final.
Sarvam STT does emit it on completed Hindi utterances.

Diagnostic logging:
  Every finalized TranscriptionFrame is logged with the vendor (inferred from
  frame.result shape), confidence, text-tail, end-of-utterance signal, and
  the gate decision (early_trigger | early_skip:<reason>). The actual
  trigger_user_turn_stopped() call is also logged with which path fired it
  (early vs fallback) and the wall-clock since the finalize was seen.

Grep recipes for the log file:
  grep "stt_finalize"     # every finalized transcript (both vendors)
  grep "early_trigger"    # fast path fired (LLM should follow immediately)
  grep "early_skip"       # gate rejected — see reason
  grep "turn_stop_fired"  # actual trigger call + path + wall-time
"""
from __future__ import annotations

import logging
import time

from pipecat.frames.frames import TranscriptionFrame, VADUserStartedSpeakingFrame
from pipecat.turns.user_stop.speech_timeout_user_turn_stop_strategy import (
    SpeechTimeoutUserTurnStopStrategy,
)

logger = logging.getLogger(__name__)


SENTENCE_FINAL_PUNCT = (".", "?", "!", "।", "。", "？", "！")


def _extract_confidence(frame: TranscriptionFrame) -> tuple[float, bool]:
    """
    Pull a per-utterance confidence score from frame.result if the vendor
    populated one. Returns (confidence, found_flag) — found_flag is False
    when the vendor didn't expose it so the caller can distinguish "1.0
    because vendor said so" from "1.0 because we defaulted".

    Vendor shapes:
      - Deepgram: result["channel"]["alternatives"][0]["confidence"]
      - Sarvam:   result.get("confidence") or absent
      - Generic:  result.confidence attribute
    """
    result = getattr(frame, "result", None)
    if result is None:
        return 1.0, False
    conf = getattr(result, "confidence", None)
    if isinstance(conf, (int, float)):
        return float(conf), True
    if isinstance(result, dict):
        if isinstance(result.get("confidence"), (int, float)):
            return float(result["confidence"]), True
        try:
            alt = result["channel"]["alternatives"][0]
            if isinstance(alt.get("confidence"), (int, float)):
                return float(alt["confidence"]), True
        except (KeyError, TypeError, IndexError):
            pass
    return 1.0, False


def _extract_eou_signal(frame: TranscriptionFrame) -> str | None:
    """
    Return a vendor-specific end-of-utterance signal name if present.
      - Deepgram: speech_final / is_final / from_finalize
      - Sarvam:   final / end_of_speech (varies; we just stringify what we find)
    Returns a compact "key=value, key=value" string for logs, or None.
    """
    logger.info(f'stt transcript {frame=}')
    result = getattr(frame, "result", None)
    if result is None:
        return None
    keys = (
        "speech_final",
        "is_final",
        "from_finalize",
        "final",
        "end_of_speech",
        "eou",
    )
    found = {}
    if isinstance(result, dict):
        for k in keys:
            if k in result:
                found[k] = result[k]
    else:
        for k in keys:
            v = getattr(result, k, None)
            if v is not None:
                found[k] = v
    if not found:
        return None
    return ", ".join(f"{k}={v}" for k, v in found.items())


def _infer_vendor(frame: TranscriptionFrame) -> str:
    """Best-effort vendor tag for logs."""
    result = getattr(frame, "result", None)
    if isinstance(result, dict):
        if "channel" in result and "metadata" in result:
            return "deepgram"
        if "transcript" in result or "language_code" in result:
            return "sarvam"
    cls = type(result).__name__ if result is not None else "none"
    return cls


class EarlyTranscriptionUserTurnStopStrategy(SpeechTimeoutUserTurnStopStrategy):
    """Fires user-turn-stopped immediately on a confident, sentence-final
    finalized transcript — bypassing VAD wait + user_speech_timeout."""

    def __init__(
        self,
        *,
        user_speech_timeout: float = 0.01,
        min_confidence: float = 0.85,
        **kwargs,
    ):
        super().__init__(user_speech_timeout=user_speech_timeout, **kwargs)
        self._min_confidence = float(min_confidence)
        self._early_fired = False
        # Set when we fire from the early path; consumed and cleared by
        # trigger_user_turn_stopped() so the next firing (fallback or new
        # turn) is correctly tagged.
        self._fire_path: str | None = None
        # Perf-clock of the most recent finalized-transcript decision, used
        # to compute the wall-time from finalize → trigger fire.
        self._last_finalize_t: float | None = None

    async def reset(self):
        await super().reset()
        self._early_fired = False
        self._fire_path = None
        self._last_finalize_t = None

    async def _handle_vad_user_started_speaking(
        self, frame: VADUserStartedSpeakingFrame
    ):
        # New turn — re-arm the early-trigger path.
        self._early_fired = False
        self._fire_path = None
        self._last_finalize_t = None
        await super()._handle_vad_user_started_speaking(frame)

    async def _handle_transcription(self, frame: TranscriptionFrame):
        # Run the parent first so _text accumulates and the parent's own
        # finalized-handling (cancelling stt_timeout etc.) still happens.
        await super()._handle_transcription(frame)

        logger.info(
            "stt_seen | finalized=%s | text_tail=%r | result_type=%s",
            frame.finalized,
            (frame.text or "")[-40:],
            type(getattr(frame, "result", None)).__name__,
        )

        # Don't trust frame.finalized — for Deepgram it's only True when the
        # server's response races-back as a Finalize ack (from_finalize=True),
        # which is unreliable when Deepgram's own endpointing emits the final
        # before our Finalize request lands. For both Deepgram and Sarvam,
        # every TranscriptionFrame that reaches us IS a final result
        # (interims go through InterimTranscriptionFrame, never here).

        # Diagnostic: log every finalized transcript regardless of gate
        # outcome so we can see what each vendor actually emits.
        vendor = _infer_vendor(frame)
        conf, conf_found = _extract_confidence(frame)
        eou = _extract_eou_signal(frame)
        text = (frame.text or "").strip()
        text_tail = text[-40:] if text else ""
        ends_punct = bool(text and text.endswith(SENTENCE_FINAL_PUNCT))

        self._last_finalize_t = time.perf_counter()
        logger.info(
            "stt_finalize | vendor=%s | conf=%.2f%s | ends_punct=%s | eou=%s | text_tail=%r",
            vendor,
            conf,
            "" if conf_found else "(default)",
            ends_punct,
            eou if eou else "n/a",
            text_tail,
        )

        if self._early_fired:
            logger.info("early_skip | reason=already_fired_this_turn")
            return
        if not text:
            logger.info("early_skip | reason=empty_text")
            return
        if conf < self._min_confidence:
            logger.info(
                "early_skip | reason=low_confidence | conf=%.2f | min=%.2f",
                conf,
                self._min_confidence,
            )
            return

        # All conditions satisfied — fire now, regardless of VAD state.
        self._early_fired = True
        self._user_speech_wait_done = True
        self._stt_wait_done = True
        self._vad_user_speaking = False
        self._fire_path = "early"

        logger.info(
            "early_trigger | vendor=%s | conf=%.2f | text_tail=%r",
            vendor,
            conf,
            text_tail,
        )
        await self._cancel_all_tasks()
        await self.trigger_user_turn_stopped()

    async def trigger_user_turn_stopped(self):
        """Log which code path fired the trigger and the wall-time from the
        last finalize. Then delegate to parent."""
        path = self._fire_path or "fallback_vad_timer"
        dt_ms = None
        if self._last_finalize_t is not None:
            dt_ms = (time.perf_counter() - self._last_finalize_t) * 1000.0
        logger.info(
            "turn_stop_fired | path=%s | since_finalize_ms=%s",
            path,
            f"{dt_ms:.1f}" if dt_ms is not None else "n/a",
        )
        # Reset for the next turn so a subsequent fallback fire isn't
        # misreported as "early".
        self._fire_path = None
        await super().trigger_user_turn_stopped()
