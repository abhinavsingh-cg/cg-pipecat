"""
config.py — all runtime knobs in one place.

Every value can be overridden by a .env file (loaded at import time) or by
setting the environment variable before running. This is the primary file
to edit when you need to change behavior without touching code.

QUICK-START: copy .env.example → .env, then set:
  STT_PRIMARY=sarvam|deepgram|credgenics_http
  LLM_VENDOR=groq|openai|bedrock|sarvam
  TTS_VENDOR=sarvam|elevenlabs|cartesia
  ...plus the corresponding API keys below.
"""
import os
from dotenv import load_dotenv

load_dotenv()

# ── Audio / RTP ──────────────────────────────────────────────────────────────
# Asterisk produces μ-law at 8 kHz / 20 ms frames. These constants flow into
# the RTP codec, transport, and STT/TTS factories. Change only if your
# Asterisk dial-plan uses a different codec or packetization.
SAMPLE_RATE = 8000
SAMPLE_WIDTH = 2           # bytes per sample (int16)
CHANNELS = 1
FRAME_DURATION_MS = 20
FRAME_SAMPLES = int(SAMPLE_RATE * FRAME_DURATION_MS / 1000)  # 160 samples/frame
ULAW_RTP_FRAME_SIZE = 160  # one 20 ms μ-law frame = 160 bytes
RTP_HEADER_SIZE = 12
RTP_TIMESTAMP_DIFFERENCE = 160   # samples per frame
RTP_SEND_INTERVAL = 0.020        # seconds between outbound RTP frames
SSRC = 12345678                  # fixed SSRC for outbound stream (arbitrary)

# ── VAD / endpointing ────────────────────────────────────────────────────────
# These tune Silero VAD (pipeline.vad_analyzer) AND Smart Turn's timeout.
# Raise VAD_MIN_SILENCE_MS if the bot triggers too early on pauses.
# Lower VAD_MIN_SPEECH_MS if it misses very short utterances.
VAD_MIN_SILENCE_MS = int(os.getenv("VAD_MIN_SILENCE_MS", "500"))
VAD_MIN_SPEECH_MS = int(os.getenv("VAD_MIN_SPEECH_MS", "200"))
VAD_CONFIDENCE = float(os.getenv("VAD_CONFIDENCE", "0.6"))

# ── Idle / "are you there?" ──────────────────────────────────────────────────
# After ARE_YOU_THERE_TIMEOUT_S of silence the bot asks "are you there?".
# After ARE_YOU_THERE_MAX_STRIKES unanswered prompts it sends EndFrame (hangup).
ARE_YOU_THERE_TIMEOUT_S = float(os.getenv("ARE_YOU_THERE_TIMEOUT", "8"))
ARE_YOU_THERE_MAX_STRIKES = int(os.getenv("ARE_YOU_THERE_MAX_STRIKES", "3"))

# ── Early barge-in concat ───────────────────────────────────────────────────
# If the user interrupts within this window of the bot starting to speak, the
# previous user transcript is stashed and prepended to the next utterance.
# EarlyBargeInConcatProcessor must be un-commented in pipeline.py to use this.
EARLY_BARGE_IN_WINDOW_S = float(os.getenv("EARLY_BARGE_IN_WINDOW_S", "0.8"))

# ── Redis / conversation memory ─────────────────────────────────────────────
# Schema: call:{call_id}:data → JSON list of {role, content, timestamp}
# Used by RedisMemory (production). WebRTC dev runner uses InMemoryMemory.
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
CONTEXT_EXPIRY_SECONDS = int(os.getenv("CONTEXT_EXPIRY_SECONDS", str(24 * 60 * 60)))

# ── Vendor selection ────────────────────────────────────────────────────────
# These three vars control which service is built by the factories.
# Adding a new vendor: edit the matching factory file and add a new branch.
LLM_VENDOR = os.getenv("LLM_VENDOR", "groq").lower()    # groq | openai | bedrock | sarvam
TTS_VENDOR = os.getenv("TTS_VENDOR", "sarvam").lower()  # sarvam | elevenlabs | cartesia
STT_PRIMARY = os.getenv("STT_PRIMARY", "sarvam").lower() # sarvam | deepgram | credgenics_http
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

# ── Internal Credgenics HTTP STT ─────────────────────────────────────────────
# POST {STT_BASE_URL}/transcribe with form fields: audio, current_language,
# supported_languages. Response: {"transcript": "...", "language": "hindi"}.
# Used when STT_PRIMARY=credgenics_http.
STT_BASE_URL = os.getenv("STT_BASE_URL", "")
STT_TIMEOUT_S = float(os.getenv("STT_TIMEOUT_S", "4"))

# ── LLM model + retries ─────────────────────────────────────────────────────
# LLM_MODEL is passed as-is to the vendor service. Groq default is the 70B
# Llama-3.1; change to "llama-3.1-8b-instant" for lower latency.
LLM_MODEL = os.getenv("LLM_MODEL", "llama-3.1-70b-versatile")
LLM_TIMEOUT_S = float(os.getenv("LLM_TIMEOUT_S", "1.5"))
MAX_LLM_RETRIES = int(os.getenv("MAX_LLM_RETRIES", "2"))

# ── TTS defaults (Sarvam) ───────────────────────────────────────────────────
# bulbul:v3 supports Hindi + 10 Indian languages.
# voice_id / speaker options: shubh, meera, arvind, amol, amartya, diya, etc.
# pace >1.0 speaks faster; temperature controls expressiveness.
SARVAM_TTS_MODEL = os.getenv("SARVAM_TTS_MODEL", "bulbul:v3")
SARVAM_TTS_SPEAKER = os.getenv("SARVAM_TTS_SPEAKER", "shubh")
SARVAM_TTS_PACE = float(os.getenv("SARVAM_TTS_PACE", "1.10"))
SARVAM_TTS_TEMPERATURE = float(os.getenv("SARVAM_TTS_TEMPERATURE", "0.6"))

# ── STT defaults ────────────────────────────────────────────────────────────
SARVAM_STT_MODEL = os.getenv("SARVAM_STT_MODEL", "saaras:v3")
# Sarvam STT requires 16 kHz input; the service resamples internally from 8 kHz.
SARVAM_STT_SAMPLE_RATE = int(os.getenv("SARVAM_STT_SAMPLE_RATE", "16000"))
DEEPGRAM_STT_MODEL = os.getenv("DEEPGRAM_STT_MODEL", "nova-2")

# ── Language ────────────────────────────────────────────────────────────────
# DEFAULT_LANGUAGE must be a key in SUPPORTED_LNG_SUFFIX below.
DEFAULT_LANGUAGE = os.getenv("DEFAULT_LANGUAGE", "hindi")
# How many consecutive turns in a new language before the bot switches.
# Higher = more stable (fewer accidental switches); lower = more responsive.
LANG_SWITCH_THRESHOLD = int(os.getenv("LANG_SWITCH_THRESHOLD", "2"))

# ISO 639-1 code ↔ internal language key.
# CUSTOMIZE: add a language here + in SUPPORTED_LNG_SUFFIX + ARE_YOU_THERE_TEXT
# in processors/are_you_there.py.
LANGUAGE_CODES = {
    "bn": "bengali", "en": "english", "hi": "hindi", "te": "telugu",
    "ur": "urdu", "ml": "malayalam", "mr": "marathi", "kn": "kannada",
    "pa": "punjabi", "ta": "tamil", "gu": "gujarati",
}
LANGUAGE_TO_CODES = {v: k for k, v in LANGUAGE_CODES.items()}
LANGUAGE_TO_CODES["panjabi"] = "pa"

# Suffix appended to user transcript when inject_suffix=True (Credgenics STT).
# The system prompt (prompts/pd_si.py:269-270) has matching hard rules.
# CUSTOMIZE: add / edit language entries here to change what the LLM receives.
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

# SpeechBrain voxlingua107 raw label → internal language key.
# Add entries here if new SpeechBrain labels need to map to an internal key.
SPEECHBRAIN_LABEL_MAP = {
    "hi": "hindi", "en": "english", "te": "telugu",
    "ml": "malayalam", "mr": "marathi", "ta": "tamil", "bn": "bengali",
    "hindi": "hindi", "english": "english", "telugu": "telugu",
    "malayalam": "malayalam", "marathi": "marathi", "tamil": "tamil",
    "bengali": "bengali",
}

# langdetect ISO code → internal language key (used as text-level LID fallback).
LANGDETECT_MAP = {
    "hi": "hindi", "en": "english", "te": "telugu",
    "ml": "malayalam", "mr": "marathi", "bn": "bengali", "ta": "tamil",
}

# Internal language key → ISO 639-1 (for Sarvam TTS target_language_code etc.)
LANG_TO_ISO = {v: k for k, v in LANGDETECT_MAP.items()}

# ── LID ─────────────────────────────────────────────────────────────────────
# How many seconds of audio to buffer per utterance for SpeechBrain LID.
# Longer = more accurate but adds latency before the language guess is ready.
LID_AUDIO_SEC = float(os.getenv("LID_AUDIO_SEC", "1.5"))
# Minimum SpeechBrain score to accept the detected language.
LID_MIN_CONFIDENCE = float(os.getenv("LID_MIN_CONFIDENCE", "0.6"))

# ── Logging / OTEL ──────────────────────────────────────────────────────────
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
LOG_FILE = os.getenv("LOG_FILE", "/tmp/voicebot.log")
OTEL_EXPORTER_OTLP_ENDPOINT = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "")
OTEL_SERVICE_NAME = os.getenv("OTEL_SERVICE_NAME", "voicebot")

# ── Local-dev WebRTC server ─────────────────────────────────────────────────
WEBRTC_HOST = os.getenv("WEBRTC_HOST", "127.0.0.1")
WEBRTC_PORT = int(os.getenv("WEBRTC_PORT", "7860"))
