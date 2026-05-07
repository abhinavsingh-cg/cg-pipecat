"""
Language state + hysteresis-gated language switching.

Ported verbatim from /Users/admin/livekit/language_state.py — the logic is
framework-agnostic and works as well in Pipecat as it did in LiveKit Agents.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from langdetect import detect as _langdetect
from langdetect.lang_detect_exception import LangDetectException

from voicebot.config import (
    LANGDETECT_MAP,
    LANG_SWITCH_THRESHOLD,
    SPEECHBRAIN_LABEL_MAP,
)


@dataclass
class LanguageState:
    current_language: str
    candidate_language: Optional[str] = None
    candidate_count: int = 0
    turn_count: int = 0
    last_switch_turn: int = 0
    call_id: str = ""

    # Languages this call is allowed to be transcribed in (Asterisk dialplan
    # var / job metadata). Used by CredgenicsHTTPSTT, language suffix logic,
    # and the LLM prompt to gate detection.
    supported_languages: list = None

    # Early-barge-in concat scratch space.
    last_user_transcription: Optional[str] = None
    pending_concat_text: Optional[str] = None
    bot_speech_start_ts: Optional[float] = None
    llm_output_inserted: bool = False

    # Idle watchdog scratch space.
    are_you_there_count: int = 0

    def __post_init__(self):
        if self.supported_languages is None:
            self.supported_languages = [self.current_language]


def normalize_speechbrain_label(raw: str) -> Optional[str]:
    """Map SpeechBrain voxlingua107 output → SUPPORTED_LNG_SUFFIX key."""
    if not raw:
        return None
    code = raw.split(":")[0].strip().lower() if ":" in raw else raw.strip().lower()
    return SPEECHBRAIN_LABEL_MAP.get(code) or SPEECHBRAIN_LABEL_MAP.get(raw.strip())


def detect_language_from_text(transcript: str) -> Optional[str]:
    """Text-level fallback when SpeechBrain hasn't fired yet (short utterance)."""
    if not transcript or len(transcript.strip()) < 5:
        return None
    try:
        code = _langdetect(transcript)
        return LANGDETECT_MAP.get(code)
    except LangDetectException:
        return None


def update_candidate(state: LanguageState, detected: str) -> bool:
    """
    Hysteresis: requires LANG_SWITCH_THRESHOLD consecutive turns in the
    detected language before the switch is committed.
    """
    if detected == state.current_language:
        state.candidate_language = None
        state.candidate_count = 0
        return False
    if detected == state.candidate_language:
        state.candidate_count += 1
    else:
        state.candidate_language = detected
        state.candidate_count = 1
    return state.candidate_count >= LANG_SWITCH_THRESHOLD


def commit_switch(state: LanguageState) -> str:
    """Commit the pending candidate. Returns the new language key."""
    new_lang = state.candidate_language
    state.last_switch_turn = state.turn_count
    state.current_language = new_lang
    state.candidate_language = None
    state.candidate_count = 0
    return new_lang
