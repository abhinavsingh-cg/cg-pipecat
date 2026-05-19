"""
Stage/node-based prompt machinery — see plan: minimize-federated-wind.md.

Two processors:

  StageOverlayProcessor
    Sits between context_aggr.user() and the LLM. Before each LLM call,
    rewrites the system overlay in LLMContext.messages[1].content to match
    the current stage. This keeps the per-turn input small (base + one
    stage overlay) instead of shipping the entire monolithic call flow.

  StageRouterProcessor
    Sits after the LLM. Streams TextFrames downstream as usual, but watches
    for a trailing `[[stage:<name>]]` marker the LLM is instructed to emit
    on its last line. The marker is stripped before it reaches the TTS;
    when LLMFullResponseEndFrame fires, the parsed stage is validated
    against known stages and committed to state.current_stage + memory.

Streaming-safety: the router holds a small lookback buffer so a marker
that spans multiple TextFrames (common with token-by-token streaming)
is still caught without leaking partial brackets to TTS.
"""
from __future__ import annotations

import logging
import re
from typing import Optional

from pipecat.frames.frames import (
    Frame,
    InterruptionFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    TextFrame,
)
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from voicebot.prompts.call_data import (
    INITIAL_STAGE,
    build_stage_prompt,
    known_stages,
)
from voicebot.state.language_state import LanguageState
from voicebot.state.memory import ConversationMemory

logger = logging.getLogger(__name__)


# Complete marker match: `[[stage:<name>]]` with optional surrounding whitespace.
_STAGE_MARKER_RE = re.compile(r"\s*\[\[\s*stage\s*:\s*([a-zA-Z_]+)\s*\]\]\s*")

# Conservative lookback: a marker is at most ~30 chars; hold that many chars
# back from each emit so we can catch markers spanning multiple TextFrames.
_LOOKBACK = 32


class StageOverlayProcessor(FrameProcessor):
    """
    Rewrites `context.messages[1].content` to the overlay for the current
    stage before each LLM call. The base prompt at index 0 is left untouched.

    Idempotent: if the overlay already matches, no work is done.
    """

    def __init__(
        self,
        state: LanguageState,
        context: LLMContext,
        call_data: Optional[dict] = None,
    ):
        super().__init__()
        self._state = state
        self._context = context
        self._call_data = call_data
        self._last_written_stage: Optional[str] = None

    def _sync_overlay(self) -> None:
        stage = self._state.current_stage or INITIAL_STAGE
        if stage == self._last_written_stage:
            return
        try:
            overlay = build_stage_prompt(stage, self._call_data)
        except Exception as exc:
            logger.warning("stage_overlay_build_failed | stage=%s | err=%s", stage, exc)
            return
        messages = self._context.messages
        if len(messages) < 2:
            logger.warning(
                "stage_overlay_skip | messages_len=%d (expected base+overlay at idx 0,1)",
                len(messages),
            )
            return
        # LLMContext stores plain dicts; mutate the overlay slot in place so
        # the same context object reused by aggregators picks up the change.
        slot = messages[1]
        if isinstance(slot, dict):
            slot["content"] = overlay
        else:
            # Some Pipecat versions wrap messages in objects with `.content`.
            try:
                slot.content = overlay  # type: ignore[attr-defined]
            except AttributeError:
                logger.warning("stage_overlay_skip | messages[1] not mutable: %r", type(slot))
                return
        self._last_written_stage = stage
        logger.info(f"""{overlay=},{self._context.messages=}""")
        logger.info("stage_overlay_applied | stage=%s", stage)

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        # Sync before any LLM trigger frame passes through. We sync on every
        # frame for safety (no-op when stage hasn't changed); the cost is one
        # string comparison per frame.
        self._sync_overlay()
        await self.push_frame(frame, direction)


class StageRouterProcessor(FrameProcessor):
    """
    Strips `[[stage:xxx]]` markers from streamed LLM TextFrames and commits
    the extracted stage to state + memory on LLMFullResponseEndFrame.
    """

    def __init__(self, state: LanguageState, memory: ConversationMemory):
        super().__init__()
        self._state = state
        self._memory = memory
        self._valid_stages = set(known_stages())
        self._buffer: str = ""
        self._pending_stage: Optional[str] = None

    def _extract_markers(self, text: str) -> tuple[str, Optional[str]]:
        """Return (text_without_markers, last_extracted_stage_or_None)."""
        last_stage: Optional[str] = None
        cleaned = text
        while True:
            m = _STAGE_MARKER_RE.search(cleaned)
            if not m:
                break
            stage = m.group(1).strip().lower()
            if stage in self._valid_stages:
                last_stage = stage
            else:
                logger.warning("stage_router_invalid | got=%r", stage)
            cleaned = cleaned[: m.start()] + cleaned[m.end():]
        return cleaned, last_stage

    async def _emit_safe_prefix(self, direction: FrameDirection) -> None:
        """Flush everything except the last _LOOKBACK chars (held back for a
        possible split marker straddling chunk boundaries)."""
        if len(self._buffer) <= _LOOKBACK:
            return
        # Find a safe split point that doesn't cut a marker in half.
        cut = len(self._buffer) - _LOOKBACK
        emit, hold = self._buffer[:cut], self._buffer[cut:]
        # First check if the emit portion contains any complete markers.
        emit, stage = self._extract_markers(emit)
        if stage:
            self._pending_stage = stage
        self._buffer = hold
        if emit:
            await self.push_frame(TextFrame(text=emit), direction)

    async def _flush_buffer(self, direction: FrameDirection) -> None:
        if not self._buffer:
            return
        text, stage = self._extract_markers(self._buffer)
        if stage:
            self._pending_stage = stage
        self._buffer = ""
        if text:
            await self.push_frame(TextFrame(text=text), direction)

    async def _commit_stage(self) -> None:
        if not self._pending_stage:
            return
        new_stage = self._pending_stage
        old_stage = self._state.current_stage
        self._pending_stage = None
        if new_stage == old_stage:
            logger.info("stage_transition | from=%s | to=%s | (no change)", old_stage, new_stage)
            return
        self._state.current_stage = new_stage
        try:
            await self._memory.set_stage(new_stage)
        except Exception as exc:
            logger.warning("stage_persist_failed | stage=%s | err=%s", new_stage, exc)
        logger.info("stage_transition | from=%s | to=%s", old_stage, new_stage)

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)

        if isinstance(frame, LLMFullResponseStartFrame):
            # New response — reset buffer & any uncommitted stage from a
            # previously interrupted turn.
            self._buffer = ""
            self._pending_stage = None
            await self.push_frame(frame, direction)
            return

        if isinstance(frame, LLMFullResponseEndFrame):
            await self._flush_buffer(direction)
            await self._commit_stage()
            await self.push_frame(frame, direction)
            return

        if isinstance(frame, InterruptionFrame):
            # Bot was interrupted — discard buffered tail & pending stage.
            self._buffer = ""
            self._pending_stage = None
            await self.push_frame(frame, direction)
            return

        if isinstance(frame, TextFrame) and frame.text:
            self._buffer += frame.text
            await self._emit_safe_prefix(direction)
            return

        await self.push_frame(frame, direction)
