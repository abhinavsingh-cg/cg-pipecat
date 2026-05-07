"""
SpeechBrain Language Identification — runs as a side-channel FrameProcessor.

Replaces the monkey-patched LIDAwareSTT in /Users/admin/livekit/lid_stt.py
with a clean FrameProcessor that sees raw audio before STT and updates
LanguageState as a side-effect. No wrapping or patching of the STT service.
"""
from __future__ import annotations

import asyncio
import logging
from typing import List, Optional

import numpy as np

from pipecat.frames.frames import (
    Frame,
    InputAudioRawFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from voicebot.config import LID_AUDIO_SEC, LID_MIN_CONFIDENCE, SAMPLE_RATE
from voicebot.state.language_state import (
    LanguageState,
    normalize_speechbrain_label,
    update_candidate,
)

logger = logging.getLogger(__name__)


class LIDProcessor(FrameProcessor):
    """
    Buffers the first ~1.5 s of each user utterance, runs SpeechBrain LID in
    a thread pool, calls update_candidate() if confidence is high enough.

    All audio frames pass through unchanged.
    """

    def __init__(self, state: LanguageState, lid_model, sample_rate: int = SAMPLE_RATE):
        super().__init__()
        self._state = state
        self._lid = lid_model
        self._sample_rate = sample_rate
        self._target_samples = int(LID_AUDIO_SEC * sample_rate)
        self._buffer: List[np.ndarray] = []
        self._collected = 0
        self._done = False
        self._task: Optional[asyncio.Task] = None

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)

        if isinstance(frame, UserStartedSpeakingFrame):
            self._reset()
        elif isinstance(frame, UserStoppedSpeakingFrame):
            # Keep the result of an in-flight LID task; just stop collecting.
            self._done = True
        elif isinstance(frame, InputAudioRawFrame) and not self._done:
            self._collect(frame.audio)

        await self.push_frame(frame, direction)

    def _reset(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
        self._buffer.clear()
        self._collected = 0
        self._done = False
        self._task = None

    def _collect(self, audio_bytes: bytes) -> None:
        pcm = np.frombuffer(audio_bytes, dtype=np.int16).astype(np.float32) / 32768.0
        self._buffer.append(pcm)
        self._collected += len(pcm)
        if self._collected >= self._target_samples:
            self._done = True
            audio = np.concatenate(self._buffer)
            self._buffer.clear()
            try:
                loop = asyncio.get_running_loop()
                self._task = loop.run_in_executor(None, self._classify_sync, audio)
            except RuntimeError:
                pass

    def _classify_sync(self, audio: np.ndarray) -> None:
        try:
            import torch
            tensor = torch.tensor(audio).unsqueeze(0)
            _out, score, _idx, label = self._lid.classify_batch(tensor)
            confidence = float(score[0])
            raw = label[0] if isinstance(label[0], str) else label[0][0]
            detected = normalize_speechbrain_label(raw)
            logger.info("LID raw=%r detected=%s confidence=%.2f", raw, detected, confidence)
            if detected and confidence >= LID_MIN_CONFIDENCE:
                # Gate against the call's allowed languages.
                if detected in self._state.supported_languages:
                    update_candidate(self._state, detected)
        except Exception as exc:
            logger.warning("SpeechBrain LID failed: %s", exc)
