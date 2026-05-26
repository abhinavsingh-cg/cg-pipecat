"""
Prefix fillers — short language-matched utterances pushed to TTS shortly after
VAD-stop, masking LLM TTFB.

Selection is keyed on `state.current_stage` (see prompts/pd_si.py STAGES) so
the filler matches the conversational context — e.g. an empathetic "जी, समझ
रही हूँ…" during `persuade`, vs. a "ठीक है, मैं नोट कर रही हूँ…" while
recording payment intent. Stages without a tuned pool fall back to
FILLERS["default"].

Pool keys accept either ISO 639-1 codes ("hi", "en") OR full names
("hindi", "english") — `state.current_language` differs across runs.
"""
from __future__ import annotations

import random
from typing import Optional

from voicebot.config import FILLER_ENABLED, FILLER_PROBABILITY, LANGUAGE_CODES


# stage → lang → list of phrases. "default" stage is the fallback when a
# stage-specific pool isn't defined for the active language. Phrases tuned
# to ~500-800ms TTS so they bridge LLM TTFB cleanly.
FILLERS: dict[str, dict[str, list[str]]] = {
    "default": {
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
    },
    "intro_verify": {
        # "hi": ["जी, हाँ…", "हम्म, बताइए…", "अच्छा, ठीक है…"],
        "hi": ["hmmm.... okay...."],
        # "en": ["yes, go ahead…", "okay, got it…", "alright…"],
        "en": ["hmmm.... okay...."],
    },
    "wrong_person": {
        "hi": ["अच्छा, ठीक है…", "जी, समझ गई…"],
        "en": ["okay, I see…", "alright, got it…"],
    },
    "inform_emi": {
        "hi": ["एक सेकंड, मैं चेक कर रही हूँ…", "हम्म, डिटेल्स देख रही हूँ…", "जी, एक मिनट रुकिए…"],
        "en": ["one second, let me check…", "okay, pulling up the details…", "alright, just a moment…"],
    },
    "payment_intent": {
        "hi": ["ठीक है, मैं नोट कर रही हूँ…", "जी, एक मिनट…", "अच्छा, समझ गई…"],
        "en": ["okay, noting that down…", "alright, just a second…", "got it, one moment…"],
    },
    "persuade": {
        "hi": ["जी, समझ रही हूँ…", "हम्म, अच्छा…", "जी, बिल्कुल…"],
        "en": ["yes, I understand…", "hmm, I see…", "okay, I hear you…"],
    },
    "dispute": {
        "hi": ["जी, एक मिनट…", "ठीक है, मैं देखती हूँ…", "अच्छा, समझ गई…"],
        "en": ["alright, let me look into that…", "okay, one moment…", "I see, just a second…"],
    },
}


def _normalize_lang(lang: str, pool: dict[str, list[str]]) -> str:
    """Accept either ISO code or full name; return ISO code or fallback 'en'."""
    if not lang:
        return "en"
    lang = lang.lower()
    if lang in pool:
        return lang
    for iso, name in LANGUAGE_CODES.items():
        if name == lang and iso in pool:
            return iso
    return "en"


# Last-used tracker keyed on (stage, iso) so repeated turns in the same stage
# don't repeat the same phrase back-to-back.
_LAST_USED: dict[tuple[str, str], str] = {}


def pick_filler(
    language: str,
    *,
    stage: str = "default",
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

    # Resolve stage pool; fall back to default if stage missing or doesn't
    # cover the active language.
    stage_pool = FILLERS.get(stage) or FILLERS["default"]
    iso = _normalize_lang(language, stage_pool)
    options = stage_pool.get(iso)
    if not options:
        # Stage exists but no entry for this language → fall back to default
        # stage's pool for that language.
        default_pool = FILLERS["default"]
        iso = _normalize_lang(language, default_pool)
        options = default_pool.get(iso) or default_pool["en"]
        stage = "default"

    key = (stage, iso)
    last = _LAST_USED.get(key)
    candidates = [o for o in options if o != last] or options
    choice = r.choice(candidates)
    _LAST_USED[key] = choice
    return choice
