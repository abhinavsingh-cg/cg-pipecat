"""
LanguageState + hysteresis-gated language switching.

LanguageState is a plain dataclass passed by *reference* to every processor
that needs to read or write the current language or barge-in scratch space.
It is NOT thread-safe — all access happens on the asyncio event loop.

HYSTERESIS FLOW:
  LIDProcessor (audio LID) or LanguageSuffixProcessor (text LID fallback)
    → calls update_candidate(state, detected_language)
    → after LANG_SWITCH_THRESHOLD consecutive matching turns:
    → calls commit_switch(state)
    → LanguageSuffixProcessor calls on_language_switch(old, new)
    → pipeline.py's callback calls tts.update_options(language=iso)

CUSTOMIZE:
  • LANG_SWITCH_THRESHOLD in config.py controls how sticky the current language
    is. 1 = switch on every turn; 3+ = very stable.
  • supported_languages filters which languages are accepted as switch targets.
    Set this from call metadata (e.g. Asterisk dialplan variable) in main.py
    before calling build_and_run().
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
    current_language: str          # e.g. "hindi" — drives TTS + LLM suffix
    last_detected_language: Optional[str] = None  # most recent STT/LID language signal
    candidate_language: Optional[str] = None   # language being tested for hysteresis
    candidate_count: int = 0       # consecutive turns detected in candidate_language
    turn_count: int = 0            # total transcript turns processed this call
    last_switch_turn: int = 0      # turn_count at last committed switch
    call_id: str = ""
    campaign_id: str = ""          # passed as a query param to the Credgenics HTTP STT

    # Languages this call is allowed to switch to (from job metadata).
    # Used by CredgenicsHTTPSTT, language suffix, and the LLM prompt.
    supported_languages: list = None

    # Incremented when the Credgenics STT returns language=="invalid"; reset on a
    # valid transcript by LanguageSuffixProcessor.
    invalid_language_count: int = 0

    # ── Early-barge-in concat scratch ────────────────────────────────────────
    last_user_transcription: Optional[str] = None  # previous turn's transcript
    pending_concat_text: Optional[str] = None      # stashed for next-turn prepend
    bot_speech_start_ts: Optional[float] = None    # monotonic time when bot started
    llm_output_inserted: bool = False              # True once LLM began streaming

    # ── Idle watchdog ─────────────────────────────────────────────────────────
    are_you_there_count: int = 0   # number of "are you there?" prompts sent

    # ── End-of-call signal ───────────────────────────────────────────────────
    # Set True by TextNormalizationProcessor when the LLM emits "| END |".
    # The post-TTS EndCallTrigger fires the hangup hook on the next
    # BotStoppedSpeakingFrame.
    end_after_speech: bool = False

    # ── Node/stage tracking (see prompts/pd_si.py STAGES) ────────────────────
    # Updated by StageRouterProcessor when the LLM emits a trailing
    # `[[stage:xxx]]` marker. Read by StageOverlayProcessor to swap the system
    # overlay in LLMContext.messages[1] before each LLM call.
    current_stage: str = "intro_verify"

    # ── Greeting gate ─────────────────────────────────────────────────────────
    # True from call start until the first BotStoppedSpeakingFrame fires.
    # While True, GreetingGate swallows InterruptionFrame /
    # UserStartedSpeakingFrame so the deterministic first message can't be
    # cancelled by VAD false-triggers. STT/aggregator continue running
    # normally — bot keeps listening throughout.
    greeting_active: bool = True

    def __post_init__(self):
        if self.supported_languages is None:
            self.supported_languages = [self.current_language]
        if self.last_detected_language is None:
            self.last_detected_language = self.current_language


def normalize_speechbrain_label(raw: str) -> Optional[str]:
    """Map SpeechBrain voxlingua107 output → SUPPORTED_LNG_SUFFIX key.

    SpeechBrain returns labels like "hi: Hindi" or just "hi". We extract
    the ISO code and look it up in SPEECHBRAIN_LABEL_MAP.
    """
    if not raw:
        return None
    code = raw.split(":")[0].strip().lower() if ":" in raw else raw.strip().lower()
    return SPEECHBRAIN_LABEL_MAP.get(code) or SPEECHBRAIN_LABEL_MAP.get(raw.strip())


def detect_language_from_text(transcript: str) -> Optional[str]:
    """Text-level LID fallback (langdetect library).

    Used by LanguageSuffixProcessor when SpeechBrain hasn't fired yet
    (e.g. utterance too short for audio LID, or --no-lid was passed).
    Returns an internal language key or None.
    """
    if not transcript or len(transcript.strip()) < 5:
        return None
    try:
        code = _langdetect(transcript)
        return LANGDETECT_MAP.get(code)
    except LangDetectException:
        return None


def update_candidate(state: LanguageState, detected: str) -> bool:
    """Record a language detection vote and return True if threshold is met.

    Consecutive detections of the same language increment candidate_count.
    A detection of the current language resets the candidate (no switch needed).
    A detection of a *different* candidate restarts the count from 1.
    """
    state.last_detected_language = detected
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
    """Commit the pending candidate language. Returns the new language key."""
    new_lang = state.candidate_language
    state.last_switch_turn = state.turn_count
    state.current_language = new_lang
    state.candidate_language = None
    state.candidate_count = 0
    return new_lang
