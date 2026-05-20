"""
AggregatorLatencyProbe — measures how long a frame takes to cross a black-box
processor (here: context_aggr.user()).

Implementation: pre stamps `_probe_t0` directly on the frame object. Post
reads it back and computes the delta. This avoids id() collisions from
Pipecat's frame pooling and any cross-turn contamination.

Frames that originate INSIDE the wrapped processor (e.g. VAD frames emitted
by the user aggregator's internal VAD analyzer) never pass through pre and
have no `_probe_t0` — post silently skips them. Their absence is a useful
signal in itself: if you don't see a frame in the probe log, it was born
inside the box.

Per-class summary is logged on each frame so you can see both the individual
crossing time and a running max for that class.
"""
from __future__ import annotations

import logging
import time
from collections import defaultdict
from typing import Iterable

from pipecat.frames.frames import (
    Frame,
    StartFrame,
    EndFrame,
    InputAudioRawFrame,
    OutputAudioRawFrame,
    TTSAudioRawFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

logger = logging.getLogger("voicebot.aggregator_probe")

# Skip high-frequency audio/control frames — never useful as latency signal.
_SKIP_TYPES = (InputAudioRawFrame, OutputAudioRawFrame, TTSAudioRawFrame, StartFrame, EndFrame)

# Skip per-name as well — UserSpeakingFrame fires every audio chunk while the
# user is speaking and floods the log.
_SKIP_NAMES = {"UserSpeakingFrame", "BotSpeakingFrame", "OutputTransportMessageUrgentFrame"}

_ATTR = "_aggregator_probe_t0"


def _should_skip(frame: Frame) -> bool:
    if isinstance(frame, _SKIP_TYPES):
        return True
    if type(frame).__name__ in _SKIP_NAMES:
        return True
    return False


class _Pre(FrameProcessor):
    def __init__(self, label: str, frame_filter: tuple):
        super().__init__()
        self._label = label
        self._filter = frame_filter

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if not _should_skip(frame) and (not self._filter or isinstance(frame, self._filter)):
            try:
                setattr(frame, _ATTR, time.perf_counter())
            except (AttributeError, TypeError):
                # Frozen dataclass / slot-only class — can't stamp. Skip.
                pass
        await self.push_frame(frame, direction)


class _Post(FrameProcessor):
    def __init__(self, label: str, frame_filter: tuple, max_per_class: dict[str, float]):
        super().__init__()
        self._label = label
        self._filter = frame_filter
        self._max = max_per_class

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if not _should_skip(frame) and (not self._filter or isinstance(frame, self._filter)):
            t0 = getattr(frame, _ATTR, None)
            cls_name = type(frame).__name__
            if t0 is not None:
                dt_ms = (time.perf_counter() - t0) * 1000
                self._max[cls_name] = max(self._max.get(cls_name, 0.0), dt_ms)
                logger.info(
                    "aggregator_probe[%s] | %s | dt=%.1fms | max=%.1fms | dir=%s",
                    self._label,
                    cls_name,
                    dt_ms,
                    self._max[cls_name],
                    direction.name,
                )
            else:
                # Frame originated INSIDE the wrapped processor (no stamp).
                # Worth noting because it tells you what the aggregator emits.
                logger.info(
                    "aggregator_probe[%s] | %s | INTERNAL (no t0) | dir=%s",
                    self._label,
                    cls_name,
                    direction.name,
                )
        await self.push_frame(frame, direction)


class AggregatorLatencyProbe:
    """
    Factory that produces a (pre, post) pair of FrameProcessors. Use:
        probe = AggregatorLatencyProbe("context_aggr_user")
        procs = [..., probe.pre(), target_processor, probe.post(), ...]
    """

    def __init__(self, label: str, only: Iterable[type[Frame]] | None = None):
        self._label = label
        self._filter: tuple = tuple(only) if only else ()
        self._max_per_class: dict[str, float] = defaultdict(float)

    def pre(self) -> FrameProcessor:
        return _Pre(self._label, self._filter)

    def post(self) -> FrameProcessor:
        return _Post(self._label, self._filter, self._max_per_class)
