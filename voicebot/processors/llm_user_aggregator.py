"""
LLM user aggregation customizations.

Pipecat can receive multiple final TranscriptionFrame objects inside one
spoken user turn. LanguageSuffixProcessor appends a language directive to each
frame for Redis visibility, but the LLM should see only one directive at the
end of the aggregated user turn.
"""
from __future__ import annotations

import logging
import re
from typing import Optional

from pipecat.frames.frames import TranscriptionFrame
from pipecat.processors.aggregators.llm_response_universal import (
    LLMUserAggregator,
    LLMUserAggregatorParams,
)
from pipecat.utils.string import TextPartForConcatenation

from voicebot.config import SUPPORTED_LNG_SUFFIX

logger = logging.getLogger(__name__)

_WHITESPACE_RE = re.compile(r"\s+")
_LANGUAGE_SUFFIXES = tuple(
    sorted(
        (suffix for suffix in SUPPORTED_LNG_SUFFIX.values() if suffix),
        key=len,
        reverse=True,
    )
)


def split_language_suffixes(text: str) -> tuple[str, Optional[str]]:
    """Remove injected language suffixes and return the latest one found."""
    latest_suffix: Optional[str] = None
    latest_pos = -1

    for suffix in _LANGUAGE_SUFFIXES:
        pos = text.rfind(suffix)
        if pos > latest_pos:
            latest_pos = pos
            latest_suffix = suffix

    cleaned = text
    for suffix in _LANGUAGE_SUFFIXES:
        cleaned = cleaned.replace(suffix, "")

    return _WHITESPACE_RE.sub(" ", cleaned).strip(), latest_suffix


class LanguageSuffixAwareLLMUserAggregator(LLMUserAggregator):
    """Aggregate STT chunks while keeping only one LLM language suffix."""

    def __init__(
        self,
        context,
        *,
        params: Optional[LLMUserAggregatorParams] = None,
        **kwargs,
    ):
        super().__init__(context=context, params=params, **kwargs)
        self._latest_language_suffix: Optional[str] = None

    async def reset(self):
        await super().reset()
        self._latest_language_suffix = None

    async def _handle_transcription(self, frame: TranscriptionFrame):
        text, suffix = split_language_suffixes(frame.text)

        if suffix:
            self._latest_language_suffix = suffix

        if not text.strip():
            return

        self._aggregation.append(
            TextPartForConcatenation(
                text, includes_inter_part_spaces=frame.includes_inter_frame_spaces
            )
        )

    async def push_aggregation(self) -> str:
        if not self._aggregation:
            self._latest_language_suffix = None
            return await super().push_aggregation()

        if self._latest_language_suffix and self._aggregation:
            self._aggregation.append(
                TextPartForConcatenation(
                    self._latest_language_suffix,
                    includes_inter_part_spaces=True,
                )
            )
            logger.debug(
                "llm_suffix_finalized | suffix=%r", self._latest_language_suffix
            )

        return await super().push_aggregation()
