"""
Voicebot configuration — env vars, language maps, vendor selectors.

Carries forward only what's still needed after the migration. Everything that
maps onto a Pipecat-native setting (VAD threshold, silence-frame counts, RMS
gates, pre-buffer sizes) lives in transport / pipeline construction, not here.
"""
import os
from dotenv import load_dotenv

load_dotenv()

# ── Audio / RTP ──────────────────────────────────────────────────────────────
SAMPLE_RATE = 8000
SAMPLE_WIDTH = 2
CHANNELS = 1
FRAME_DURATION_MS = 20
FRAME_SAMPLES = int(SAMPLE_RATE * FRAME_DURATION_MS / 1000)  # 160
ULAW_RTP_FRAME_SIZE = 160
RTP_HEADER_SIZE = 12
RTP_TIMESTAMP_DIFFERENCE = 160
RTP_SEND_INTERVAL = 0.020
SSRC = 12345678

# ── VAD / endpointing — passed straight to SileroVADAnalyzer / SmartTurn ─────
VAD_MIN_SILENCE_MS = int(os.getenv("VAD_MIN_SILENCE_MS", "500"))
VAD_MIN_SPEECH_MS = int(os.getenv("VAD_MIN_SPEECH_MS", "200"))
VAD_CONFIDENCE = float(os.getenv("VAD_CONFIDENCE", "0.6"))

# ── Idle / "are you there?" ──────────────────────────────────────────────────
ARE_YOU_THERE_TIMEOUT_S = float(os.getenv("ARE_YOU_THERE_TIMEOUT", "8"))
ARE_YOU_THERE_MAX_STRIKES = int(os.getenv("ARE_YOU_THERE_MAX_STRIKES", "3"))

# ── Early barge-in concat ───────────────────────────────────────────────────
EARLY_BARGE_IN_WINDOW_S = float(os.getenv("EARLY_BARGE_IN_WINDOW_S", "0.8"))

# ── Redis / conversation memory ─────────────────────────────────────────────
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
CONTEXT_EXPIRY_SECONDS = int(os.getenv("CONTEXT_EXPIRY_SECONDS", str(24 * 60 * 60)))

# ── Vendor selection ────────────────────────────────────────────────────────
LLM_VENDOR = os.getenv("LLM_VENDOR", "groq").lower()    # groq | openai | bedrock | sarvam
TTS_VENDOR = os.getenv("TTS_VENDOR", "sarvam").lower()  # sarvam | elevenlabs | cartesia
STT_PRIMARY = os.getenv("STT_PRIMARY", "sarvam").lower()
STT_FALLBACK_CHAIN = [v.strip() for v in os.getenv(
    "STT_FALLBACK_CHAIN", "sarvam,deepgram,credgenics_http"
).split(",") if v.strip()]
STT_VENDOR_TIMEOUT_S = float(os.getenv("STT_VENDOR_TIMEOUT_S", "4"))

# ── API keys ────────────────────────────────────────────────────────────────
SARVAM_API_KEY = os.getenv("SARVAM_API_KEY", "")
DEEPGRAM_API_KEY = os.getenv("DEEPGRAM_API_KEY", "")
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
ELEVEN_API_KEY = os.getenv("ELEVEN_API_KEY", "")
ELEVENLABS_VOICE_ID = os.getenv("ELEVENLABS_VOICE_ID", "")
CARTESIA_API_KEY = os.getenv("CARTESIA_API_KEY", "")
CARTESIA_VOICE_ID = os.getenv("CARTESIA_VOICE_ID", "")
AWS_BEDROCK_REGION = os.getenv("AWS_BEDROCK_REGION", "us-east-1")
AWS_BEDROCK_API_KEY = os.getenv("AWS_BEDROCK_API_KEY", "")
SARVAM_LLM_API_KEY = os.getenv("SARVAM_LLM_API_KEY", SARVAM_API_KEY)

# Internal Credgenics HTTP STT
STT_BASE_URL = os.getenv("STT_BASE_URL", "")
STT_TIMEOUT_S = float(os.getenv("STT_TIMEOUT_S", "4"))

# ── LLM model + retries ─────────────────────────────────────────────────────
LLM_MODEL = os.getenv("LLM_MODEL", "llama-3.1-70b-versatile")
LLM_TIMEOUT_S = float(os.getenv("LLM_TIMEOUT_S", "1.5"))
MAX_LLM_RETRIES = int(os.getenv("MAX_LLM_RETRIES", "2"))

# ── TTS defaults (Sarvam) ───────────────────────────────────────────────────
SARVAM_TTS_MODEL = os.getenv("SARVAM_TTS_MODEL", "bulbul:v3")
SARVAM_TTS_SPEAKER = os.getenv("SARVAM_TTS_SPEAKER", "shubh")
SARVAM_TTS_PACE = float(os.getenv("SARVAM_TTS_PACE", "1.10"))
SARVAM_TTS_TEMPERATURE = float(os.getenv("SARVAM_TTS_TEMPERATURE", "0.6"))

# ── STT defaults ────────────────────────────────────────────────────────────
SARVAM_STT_MODEL = os.getenv("SARVAM_STT_MODEL", "saaras:v3")
SARVAM_STT_SAMPLE_RATE = int(os.getenv("SARVAM_STT_SAMPLE_RATE", "16000"))
DEEPGRAM_STT_MODEL = os.getenv("DEEPGRAM_STT_MODEL", "nova-2")

# ── Language ────────────────────────────────────────────────────────────────
DEFAULT_LANGUAGE = os.getenv("DEFAULT_LANGUAGE", "hindi")
LANG_SWITCH_THRESHOLD = int(os.getenv("LANG_SWITCH_THRESHOLD", "2"))

# ISO ↔ language-name maps (port from cg_voicebot/config.py:92-119).
LANGUAGE_CODES = {
    "bn": "bengali", "en": "english", "hi": "hindi", "te": "telugu",
    "ur": "urdu", "ml": "malayalam", "mr": "marathi", "kn": "kannada",
    "pa": "punjabi", "ta": "tamil", "gu": "gujarati",
}
LANGUAGE_TO_CODES = {v: k for k, v in LANGUAGE_CODES.items()}
LANGUAGE_TO_CODES["panjabi"] = "pa"

# Language suffix appended to the user message — the system prompt has hard
# rules (pd_si.py:269-270) honoring these.
SUPPORTED_LNG_SUFFIX = {
    "english":   " : Reply to this in English language",
    "hindi":     " : इसका जवाब हिंदी भाषा में दे",
    "telugu":    " : దీనికి తెలుగులో సమాధానం ఇవ్వండి",
    "malayalam": " : ഇതിന് മലയാളത്തിൽ മറുപടി നൽകുക.",
    "marathi":   " : याला मराठीत उत्तर द्या.",
    "tamil":     " : இதற்குத் தமிழில் பதிலளிக்கவும்.",
    "bengali":   " : এর জবাবে বাংলায় লিখুন।",
}
SUPPORTED_LANGUAGES = set(SUPPORTED_LNG_SUFFIX.keys())

# SpeechBrain voxlingua107 label → internal language key.
SPEECHBRAIN_LABEL_MAP = {
    "hi": "hindi", "en": "english", "te": "telugu",
    "ml": "malayalam", "mr": "marathi", "ta": "tamil", "bn": "bengali",
    "hindi": "hindi", "english": "english", "telugu": "telugu",
    "malayalam": "malayalam", "marathi": "marathi", "tamil": "tamil",
    "bengali": "bengali",
}

# langdetect ISO code → internal language key.
LANGDETECT_MAP = {
    "hi": "hindi", "en": "english", "te": "telugu",
    "ml": "malayalam", "mr": "marathi", "bn": "bengali", "ta": "tamil",
}

# Internal language key → ISO 639-1 (for Sarvam TTS target_language_code etc.)
LANG_TO_ISO = {v: k for k, v in LANGDETECT_MAP.items()}

# ── LID ─────────────────────────────────────────────────────────────────────
LID_AUDIO_SEC = float(os.getenv("LID_AUDIO_SEC", "1.5"))
LID_MIN_CONFIDENCE = float(os.getenv("LID_MIN_CONFIDENCE", "0.6"))

# ── Logging / OTEL ──────────────────────────────────────────────────────────
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
# Default to /tmp/voicebot.log so logs are always captured for debugging.
LOG_FILE = os.getenv("LOG_FILE", "/tmp/voicebot.log")
OTEL_EXPORTER_OTLP_ENDPOINT = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "")
OTEL_SERVICE_NAME = os.getenv("OTEL_SERVICE_NAME", "voicebot")

# ── Local-dev WebRTC server ─────────────────────────────────────────────────
WEBRTC_HOST = os.getenv("WEBRTC_HOST", "127.0.0.1")
WEBRTC_PORT = int(os.getenv("WEBRTC_PORT", "7860"))
