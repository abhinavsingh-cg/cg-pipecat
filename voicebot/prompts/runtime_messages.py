"""
Localized runtime prompts used by watchdogs and recovery flows.
"""
from __future__ import annotations

from voicebot.config import LANGUAGE_CODES

ARE_YOU_THERE_TEXT = {
    "english": "Hello, are you there?",
    "hindi": "क्या आप मेरी बात सुन पा रहे हैं?",
    "telugu": "మీరు నా మాట వినగలరా?",
    "kannada": "ನೀವು ನನ್ನ ಮಾತು ಕೇಳುತ್ತೀರಾ?",
    "tamil": "நீங்கள் என் பேச்சை கேட்கிறீர்களா?",
    "bengali": "আপনি কি আমার কথা শুনতে পাচ্ছেন?",
    "malayalam": "നിങ്ങൾ എന്റെ ശബ്ദം കേൾക്കുന്നുണ്ടോ?",
    "marathi": "तुम्ही माझं बोलणं ऐकू शकता का?",
    "gujarati": "શું તમે મારી વાત સાંભળી શકો છો?",
    "punjabi": "ਕੀ ਤੁਸੀਂ ਮੇਰੀ ਗੱਲ ਸੁਣ ਸਕਦੇ ਹੋ?",
    "panjabi": "ਕੀ ਤੁਸੀਂ ਮੇਰੀ ਗੱਲ ਸੁਣ ਸਕਦੇ ਹੋ?",
    "urdu": "کیا آپ میری بات سن رہے ہیں؟",
}

REPEAT_MESSAGE = {
    "english": "Sorry, could you please repeat that?",
    "hindi": "माफ कीजिए, क्या आप एक बार फिर से बोल सकते हैं?",
    "telugu": "క్షమించండి, దయచేసి దాన్ని మళ్ళీ చెప్పగలరా?",
    "kannada": "ಕ್ಷಮಿಸಿ, ದಯವಿಟ್ಟು ಅದನ್ನು ಪುನರಾವರ್ತಿಸಬಹುದೇ?",
    "tamil": "மன்னிக்கவும், தயவுசெய்து அதை மீண்டும் கூற முடியுமா?",
    "marathi": "माफ करा, कृपया ते पुन्हा सांगू शकाल का?",
    "gujarati": "માફ કરશો, કૃપા કરીને તેને ફરીથી કહી શકશો?",
    "malayalam": "ക്ഷമിക്കണം, ദയവായി അത് വീണ്ടും പറയാമോ?",
    "bengali": "দুঃখিত, আপনি কি আবার বলতে পারবেন?",
    "panjabi": "ਮਾਫ ਕਰਨਾ, ਕੀ ਤੁਸੀਂ ਇਸਨੂੰ ਦੁਬਾਰਾ ਕਹਿ ਸਕਦੇ ਹੋ?",
    "punjabi": "ਮਾਫ ਕਰਨਾ, ਕੀ ਤੁਸੀਂ ਇਸਨੂੰ ਦੁਬਾਰਾ ਕਹਿ ਸਕਦੇ ਹੋ?",
}


def normalize_runtime_language(language_key: str | None) -> str:
    raw = (language_key or "english").strip().lower()
    return LANGUAGE_CODES.get(raw, raw)


def localized_runtime_message(messages: dict[str, str], language_key: str | None) -> str:
    normalized = normalize_runtime_language(language_key)
    return (
        messages.get(normalized)
        or messages.get(language_key or "")
        or messages["english"]
    )
