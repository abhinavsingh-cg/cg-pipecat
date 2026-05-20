"""
Prefix fillers — short language-matched utterances pushed to TTS the instant
UserStoppedSpeakingFrame fires, masking LLM TTFB.

Single pool per language. Earlier we had an acknowledge/thinking split keyed
on user-utterance length, but `TranscriptionFrame` arrives ~500ms AFTER
`UserStoppedSpeakingFrame`, so the transcript isn't available at filler-pick
time. One tuned pool (~500-800ms phrases) bridges LLM TTFB more reliably.

Pool keys accept either ISO 639-1 codes ("hi", "en") OR full names
("hindi", "english") — `state.current_language` differs across runs.
"""
from __future__ import annotations

import random
from typing import Optional

from voicebot.config import FILLER_ENABLED, FILLER_PROBABILITY, LANGUAGE_CODES


# Phrases tuned to ~500-800ms TTS (3-5 syllables, comma-paused two-clause
# structure with a trailing ellipsis so the TTS naturally tails into the
# LLM stream that follows).
FILLERS: dict[str, list[str]] = {
    "hi": ["हम्म, एक सेकंड रुकिए…", "अच्छा, मैं देख रही हूँ…", "ठीक है, एक मिनट…", "जी, बिल्कुल सुन रही हूँ…"],
    "en": ["hmm, one moment please…", "alright, let me check…", "okay, just a second…", "yes, I hear you…"],
    "te": ["హ్మ్మ్, ఒక్క నిమిషం…", "సరే, చూస్తున్నాను…"],
    "kn": ["ಹ್ಮ್, ಒಂದು ನಿಮಿಷ ತಡೆಯಿರಿ…", "ಸರಿ, ನೋಡುತ್ತಿದ್ದೇನೆ…"],
    "ta": ["ம்ம், ஒரு நிமிஷம்…", "சரி, பார்க்கிறேன்…"],
    "bn": ["হুম, এক মিনিট দাঁড়ান…", "আচ্ছা, দেখছি…"],
    "ml": ["ഹം, ഒരു നിമിഷം…", "ശരി, നോക്കാം…"],
    "mr": ["हम्म, एक मिनिट थांबा…", "ठीक आहे, बघते…"],
    "gu": ["હમ્મ, એક મિનિટ રાહ જુઓ…", "સારું, જોઈ રહી છું…"],
    "pa": ["ਹਮ੍ਮ, ਇੱਕ ਮਿੰਟ ਰੁਕੋ…", "ਠੀਕ ਹੈ, ਵੇਖ ਰਹੀ ਹਾਂ…"],
    "ur": ["ہمم، ایک منٹ رکیے…", "اچھا، دیکھ رہی ہوں…"],
}


def _normalize_lang(lang: str) -> str:
    """Accept either ISO code or full name; return ISO code or fallback 'en'."""
    if not lang:
        return "en"
    lang = lang.lower()
    if lang in FILLERS:
        return lang
    # Full-name → ISO lookup.
    for iso, name in LANGUAGE_CODES.items():
        if name == lang:
            return iso
    return "en"


# Per-language last-used tracker so back-to-back turns don't repeat the same
# phrase.
_LAST_USED: dict[str, str] = {}


def pick_filler(
    language: str,
    *,
    end_of_call: bool = False,
    rng: Optional[random.Random] = None,
) -> Optional[str]:
    """
    Choose a filler string, or return None when:
      - FILLER_ENABLED is False
      - end_of_call is True (previous bot reply was a goodbye)
      - probabilistic gate misses (FILLER_PROBABILITY < 1.0)
    """
    if not FILLER_ENABLED or end_of_call:
        return None

    r = rng or random
    if FILLER_PROBABILITY < 1.0 and r.random() > FILLER_PROBABILITY:
        return None

    iso = _normalize_lang(language)
    options = FILLERS.get(iso) or FILLERS["en"]

    last = _LAST_USED.get(iso)
    candidates = [o for o in options if o != last] or options
    choice = r.choice(candidates)
    _LAST_USED[iso] = choice
    return choice
