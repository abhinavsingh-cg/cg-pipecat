"""
ThinkTagFilterProcessor — strips <think ...>...</think /> reasoning blocks
from LLM streaming output before they reach TTS.

Qwen-3-32b with reasoning_format="parsed" emits <think ...>reasoning</think />
inside delta.content. Pipecat pushes all delta.content as TextFrames → TTS
speaks the reasoning aloud. This processor buffers chunks and strips those
blocks before they reach TTS.

Only activates when LLM_MODEL contains "qwen". No-op for other models.
Insert AFTER the LLM, BEFORE TTS in the pipeline.
"""
from __future__ import annotations

import logging
import re

from pipecat.frames.frames import (
    Frame,
    InterruptionFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    TextFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from voicebot.config import LLM_MODEL

logger = logging.getLogger(__name__)

# Complete <think...>...</think /> block — handles multiline reasoning.
# Matches: <think reasoning_effort="none">...</think >
#          <think reasoning_effort="none">...</think />
_THINK_BLOCK_RE = re.compile(r"<think[^>]*>.*?</think[^>]*>", re.DOTALL)


class ThinkTagFilterProcessor(FrameProcessor):
    """
    Strips <think ...>...</think /> reasoning tags from LLM TextFrames.

    Uses a single buffer as state — no separate "inside_think" flag needed.
    An unclosed <think in the buffer means we're inside a think block.
    """

    def __init__(self, model_name: str | None = None):
        super().__init__()
        name = (model_name or LLM_MODEL).lower()
        self._active = "qwen" in name
        self._buf = ""
        if self._active:
            logger.info("ThinkTagFilter | active | model=%s", model_name or LLM_MODEL)
        else:
            logger.debug("ThinkTagFilter | inactive | model=%s", model_name or LLM_MODEL)

    def _drain(self) -> str:
        """Process buffer, return safe-to-emit text, hold uncertain tail."""
        # 1. Strip complete blocks
        self._buf = _THINK_BLOCK_RE.sub("", self._buf)
        if not self._buf:
            return ""

        # 2. Complete open tag found (e.g. <think reasoning_effort="none">)
        m = re.search(r"<think[^>]*>", self._buf)
        if m:
            safe = self._buf[:m.start()]
            tail = self._buf[m.end():]
            cm = re.search(r"</think[^>]*>", tail)
            if cm:
                # Block closes within buffer — strip and re-drain
                self._buf = tail[cm.end():]
                return safe + self._drain()
            # Unclosed — hold everything from the open tag onward
            self._buf = self._buf[m.start():]
            return safe

        # 3. Incomplete open tag — <think... without closing > yet
        m = re.search(r"<think(?![^>]*>)", self._buf)
        if m:
            safe = self._buf[:m.start()]
            self._buf = self._buf[m.start():]
            return safe

        # 4. Partial <think prefix at tail (e.g. <thi)
        lower = self._buf.lower()
        for k in range(min(len("<think") - 1, len(self._buf)), 0, -1):
            if lower[-k:] == "<think"[:k]:
                safe = self._buf[:-k]
                self._buf = self._buf[-k:]
                return safe

        # 5. Nothing think-related — emit all
        safe = self._buf
        self._buf = ""
        return safe

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)

        if not self._active:
            await self.push_frame(frame, direction)
            return

        if isinstance(frame, LLMFullResponseStartFrame):
            self._buf = ""
            await self.push_frame(frame, direction)
            return

        if isinstance(frame, LLMFullResponseEndFrame):
            if self._buf:
                # Final flush — strip complete blocks, discard any unclosed <think
                self._buf = _THINK_BLOCK_RE.sub("", self._buf)
                m = re.search(r"<think", self._buf)
                if m:
                    self._buf = self._buf[:m.start()]
                flushed = self._buf
                self._buf = ""
                if flushed.strip():
                    await self.push_frame(TextFrame(text=flushed), direction)
            await self.push_frame(frame, direction)
            return

        if isinstance(frame, InterruptionFrame):
            self._buf = ""
            await self.push_frame(frame, direction)
            return

        if isinstance(frame, TextFrame) and frame.text:
            self._buf += frame.text
            result = self._drain()
            if result.strip():
                frame.text = result
                await self.push_frame(frame, direction)
            return

        await self.push_frame(frame, direction)
