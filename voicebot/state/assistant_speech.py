"""
Helpers for estimating how much assistant text was actually spoken.

The TTS service can be interrupted mid-utterance, so "LLM finished" is not
the same as "caller heard the whole response". These helpers let a delivery-
side processor publish a growing prefix of the assistant message as audio
frames are successfully sent.
"""
from __future__ import annotations

from dataclasses import dataclass


_BOUNDARY_CHARS = {" ", "\t", "\n", "\r", ".", ",", "!", "?", ";", ":", "।"}


def spoken_text_prefix(text: str, ratio: float, lookback: int = 24) -> str:
    """
    Return a readable prefix of ``text`` for the delivered-audio ratio.

    We estimate spoken coverage by proportional length, then try to snap back
    to a nearby whitespace / punctuation boundary so Redis updates stay human-
    readable instead of slicing words in half whenever possible.
    """
    normalized = text.strip()
    if not normalized or ratio <= 0:
        return ""
    if ratio >= 1:
        return normalized

    cutoff = max(1, min(len(normalized), int(len(normalized) * ratio)))
    if cutoff >= len(normalized):
        return normalized

    lower_bound = max(0, cutoff - lookback)
    for idx in range(cutoff, lower_bound, -1):
        if normalized[idx - 1] in _BOUNDARY_CHARS:
            return normalized[:idx].rstrip()
    return normalized[:cutoff].rstrip()


class AssistantSpeechTracker:
    """
    Tracks assistant source text and delivered audio progress per utterance.

    Multiple assistant utterances can queue up while the current one is still
    speaking (for example when the user replies during the greeting). We keep a
    FIFO so audio delivery always updates the oldest in-flight utterance.
    """

    def __init__(self, frame_batch: int = 20):
        self._frame_batch = frame_batch
        self._queue: list[_UtteranceState] = []

    def start_llm_response(self) -> None:
        self._queue.append(_UtteranceState(collecting_llm=True))

    def add_text_chunk(self, text: str) -> None:
        if not text:
            return
        utterance = self._current_source_utterance()
        if utterance is None:
            utterance = _UtteranceState()
            self._queue.append(utterance)
        utterance.current_text += text

    def finish_llm_response(self) -> None:
        utterance = self._current_source_utterance()
        if utterance is not None:
            utterance.collecting_llm = False
            utterance.source_complete = True

    def start_tts_utterance(self, text: str) -> None:
        normalized = text.strip()
        if not normalized:
            return
        self._queue.append(
            _UtteranceState(
                current_text=normalized,
                source_complete=True,
            )
        )

    def note_generated_audio_frame(self) -> None:
        utterance = self._current_audio_utterance()
        if utterance is None:
            return
        utterance.generated_frames += 1
        if utterance.current_text:
            utterance.generated_text = utterance.current_text

    def note_delivered_audio_frame(self) -> str | None:
        utterance = self._current_audio_utterance()
        if utterance is None:
            return None
        utterance.delivered_frames += 1
        if utterance.delivered_frames - utterance.last_published_frame < self._frame_batch:
            return None
        return self._publishable_text(utterance, force=False)

    def flush_current(self) -> str | None:
        utterance = self._current_audio_utterance()
        if utterance is None:
            return None
        return self._publishable_text(utterance, force=True)

    def finish_current_utterance(self) -> None:
        if self._queue:
            self._queue.pop(0)

    def _current_audio_utterance(self) -> _UtteranceState | None:
        if not self._queue:
            return None
        return self._queue[0]

    def _current_source_utterance(self) -> _UtteranceState | None:
        if not self._queue:
            return None
        return self._queue[-1]

    def _publishable_text(self, utterance: "_UtteranceState", force: bool) -> str | None:
        candidate = self._estimate_spoken_text(utterance)
        if not candidate or candidate == utterance.last_published_text:
            if force:
                utterance.last_published_frame = utterance.delivered_frames
            return None
        utterance.last_published_frame = utterance.delivered_frames
        utterance.last_published_text = candidate
        return candidate

    def _estimate_spoken_text(self, utterance: "_UtteranceState") -> str:
        if utterance.delivered_frames <= 0:
            return ""

        full_text = (utterance.current_text or utterance.generated_text).strip()
        if not full_text:
            return ""

        if utterance.source_complete and utterance.generated_frames <= utterance.delivered_frames:
            return full_text

        text_basis = (utterance.generated_text or utterance.current_text).strip()
        if not text_basis:
            return ""

        total_frames = max(utterance.generated_frames, utterance.delivered_frames, 1)
        ratio = min(1.0, utterance.delivered_frames / total_frames)
        return spoken_text_prefix(text_basis, ratio)


@dataclass
class _UtteranceState:
    current_text: str = ""
    generated_text: str = ""
    collecting_llm: bool = False
    source_complete: bool = False
    generated_frames: int = 0
    delivered_frames: int = 0
    last_published_frame: int = 0
    last_published_text: str = ""
