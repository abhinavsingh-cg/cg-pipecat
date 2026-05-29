"""
Localized runtime prompts used by recovery flows (RepeatPromptOnFailure).
"""
from __future__ import annotations

from voicebot.config import LANGUAGE_CODES

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


INVALID_LANGUAGE_DETECTION = {
    "english": "Sorry, I can only speak in the supported language",
    "hindi": "माफ़ कीजिये, मुझे केवल समर्थित भाषा ही आती है",
    "telugu": "క్షమించండి, నాకు సమర్థించబడిన భాష మాత్రమే మాట్లాడటం వస్తుంది",
    "kannada": "ಕ್ಷಮಿಸಿ, ನಾನು ಬೆಂಬಲಿತ ಭಾಷೆಯಲ್ಲಿ ಮಾತ್ರ ಮಾತನಾಡಬಲ್ಲೆ",
    "tamil": "மன்னிக்கவும், நான் ஆதரிக்கப்படும் மொழியில் மட்டுமே பேச முடியும்",
    "marathi": "माफ करा, मला फक्त समर्थित भाषा येते",
    "gujarati": "માફ કરશો, મને માત્ર સમર્થિત ભાષા જ આવે છે",
    "malayalam": "ക്ഷമിക്കണം, എനിക്ക് പിന്തുണയുള്ള ഭാഷ മാത്രം സംസാരിക്കാൻ അറിയാം",
    "bengali": "দুঃখিত, আমি শুধুমাত্র সমর্থিত ভাষায় কথা বলতে পারি",
    "punjabi": "ਮਾਫ ਕਰਨਾ, ਮੈਂ ਸਿਰਫ਼ ਸਮਰਥਿਤ ਭਾਸ਼ਾ ਵਿੱਚ ਹੀ ਗੱਲ ਕਰ ਸਕਦਾ/ਸਕਦੀ ਹਾਂ",
    "en": "Sorry, I can only speak in the supported language",
    "hi": "माफ़ कीजिये, मुझे केवल समर्थित भाषा ही आती है",
    "te": "క్షమించండి, నాకు సమర్థించబడిన భాష మాత్రమే మాట్లాడటం వస్తుంది",
    "kn": "ಕ್ಷಮಿಸಿ, ನಾನು ಬೆಂಬಲಿತ ಭಾಷೆಯಲ್ಲಿ ಮಾತ್ರ ಮಾತನಾಡಬಲ್ಲೆ",
    "ta": "மன்னிக்கவும், நான் ஆதரிக்கப்படும் மொழியில் மட்டுமே பேச முடியும்",
    "mr": "माफ करा, मला फक्त समर्थित भाषा येते",
    "gu": "માફ કરશો, મને માત્ર સમર્થિત ભાષા જ આવે છે",
    "ml": "ക്ഷമിക്കണം, എനിക്ക് പിന്തുണയുള്ള ഭാഷ മാത്രം സംസാരിക്കാൻ അറിയാം",
    "bn": "দুঃখিত, আমি শুধুমাত্র সমর্থিত ভাষায় কথা বলতে পারি",
    "pa": "ਮਾਫ ਕਰਨਾ, ਮੈਂ ਸਿਰਫ਼ ਸਮਰਥਿਤ ਭਾਸ਼ਾ ਵਿੱਚ ਹੀ ਗੱਲ ਕਰ ਸਕਦਾ/ਸਕਦੀ ਹਾਂ",
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
