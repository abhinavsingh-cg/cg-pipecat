"""
pipeline.py — the heart of voicebot.

FRAME FLOW (downstream, left → right):
  transport.input()
    → EventLogger("input")      # logs VAD / speaking / interruption events
    → [LIDProcessor]            # optional SpeechBrain language ID on raw audio
    → stt                       # VAD-gated: buffers audio, fires TranscriptionFrame
    → TextNormalizationProcessor # digits → words; logs raw/stt_lang/current_lang/normalized
    → LanguageSuffixProcessor   # text-level LID fallback + language-switch hysteresis
                                #   (skips LID for ≤3-word utterances)
    → RedisUserRecorder         # persist user turn to Redis
    → context_aggr.user()       # accumulates transcript → LLMContext; owns VAD +
                                #   interruption broadcast (must receive audio frames)
    → llm                       # streams TextFrames
    → RedisAssistantRecorder    # persist assistant turn to Redis
    → tts                       # converts text → OutputAudioRawFrame
    → AreYouThereWatchdog       # idle-prompt timer; resets on user speech / bot speech
    → EventLogger("output")
    → transport.output()        # sends audio to caller (WebRTC or Asterisk RTP)
    → context_aggr.assistant()  # commits the assistant turn into LLMContext

KEY CUSTOMIZATION POINTS:
  • Add a new processing step: create a FrameProcessor subclass in processors/,
    instantiate it here, and insert it in the `procs` list at the right position.
  • Change VAD sensitivity: edit VAD_MIN_SPEECH_MS / VAD_MIN_SILENCE_MS / VAD_CONFIDENCE
    in config.py, or replace SileroVADAnalyzer entirely in vad_analyzer().
  • Disable Smart Turn (use pure VAD for end-of-turn): return None from turn_analyzer()
    or set SMART_TURN=false env var — the code below handles a None turn analyzer.
  • Change language detection: swap the LIDProcessor (SpeechBrain) for anything that
    calls update_candidate() on LanguageState. Disable entirely with --no-lid.
  • Change memory backend: pass a different ConversationMemory impl to build_and_run.
    InMemoryMemory is used by run_webrtc; RedisMemory by main. They share the same
    protocol so you can swap in a Postgres / DynamoDB store without touching this file.
"""
from __future__ import annotations

import logging
# Format to include 3-digit milliseconds
logging.basicConfig(
    format='%(asctime)s.%(msecs)03d %(levelname)s %(message)s',
    datefmt='%H:%M:%S'
)
# Silence noisy loggers while debugging latency. Comment out individually to
# bring any of them back.
logging.getLogger("pipecat.transports.smallwebrtc.connection").setLevel(logging.ERROR)
logging.getLogger("pipecat.transports.smallwebrtc.transport").setLevel(logging.WARNING)
logging.getLogger("aioice").setLevel(logging.WARNING)
logging.getLogger("aiortc").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)  # silence Groq HTTP 200 OK lines
from typing import Optional

from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.frames.frames import TTSSpeakFrame
from pipecat.turns.user_stop import (
    SpeechTimeoutUserTurnStopStrategy,
    TurnAnalyzerUserTurnStopStrategy,
)
from pipecat.turns.user_stop.base_user_turn_stop_strategy import BaseUserTurnStopStrategy
from pipecat.turns.user_turn_strategies import (
    UserTurnStrategies,
    default_user_turn_start_strategies,
)

from voicebot.turns.early_user_stop import EarlyTranscriptionUserTurnStopStrategy
from voicebot.turns.sarvam_turn_analyzer import SarvamTurnAnalyzerUserTurnStopStrategy
from voicebot.turns.sarvam_vad_stop import SarvamVADUserTurnStopStrategy

from voicebot.config import (
    CALL_RECORDING_ENABLED,
    DEFAULT_LANGUAGE,
    EARLY_TRIGGER_MIN_CONF,
    LANG_TO_ISO,
    SAMPLE_RATE,
    SMART_TURN_PROB_THRESHOLD,
    STT_CAMPAIGN_ID,
    STT_PRIMARY,
    SUPPORTED_LANGUAGES,
    TURN_DETECTION_MODE,
    TTS_VENDOR,
    USER_SPEECH_TIMEOUT_MS,
    VAD_CONFIDENCE,
    VAD_MIN_SILENCE_MS,
    VAD_MIN_SPEECH_MS,
)
from voicebot.processors.aggregator_probe import AggregatorLatencyProbe
from voicebot.processors.are_you_there import AreYouThereWatchdog
from voicebot.processors.early_barge_in import EarlyBargeInConcatProcessor
from voicebot.processors.end_call import EndCallTrigger
from voicebot.processors.filler_injector import FillerInjectorProcessor
from voicebot.processors.event_logger import EventLogger
from voicebot.processors.frame_tap import FrameTap
from voicebot.processors.greeting_gate import GreetingDoneFlag, GreetingGate
from voicebot.processors.language_suffix import LanguageSuffixProcessor
from voicebot.processors.redis_recorder import RedisAssistantRecorder, RedisUserRecorder
from voicebot.processors.repeat_prompt import RepeatPromptOnFailure
from voicebot.processors.stage_overlay import StageOverlayProcessor, StageRouterProcessor
from voicebot.processors.stereo_recorder import StereoCallRecorder
from voicebot.processors.text_normalizer import TextNormalizationProcessor
from voicebot.processors.turn_latency import LLMTimingProbe, TurnLatencyTracker, _Shared as _LatencyShared
from voicebot.prompts.call_data import (
    INITIAL_STAGE,
    build_base_prompt,
    build_first_message,
    build_stage_prompt,
)
from voicebot.services.llm_factory import build_llm
from voicebot.services.tts_factory import build_tts
from voicebot.state.language_state import LanguageState
from voicebot.state.memory import ConversationMemory
from voicebot.state.redis_memory import RedisMemory
from voicebot.stt.factory import build_stt
from voicebot.stt.lid import LIDProcessor

logger = logging.getLogger("voicebot")


def vad_analyzer() -> SileroVADAnalyzer:
    """
    Silero VAD — detects speech onset and offset inside user audio.

    CUSTOMIZE: tune start_secs / stop_secs / confidence via .env:
      VAD_MIN_SPEECH_MS  default 200 — ms of speech before VAD fires "started"
      VAD_MIN_SILENCE_MS default 500 — ms of silence before VAD fires "stopped"
      VAD_CONFIDENCE     default 0.6 — Silero score threshold [0..1]

    Higher start_secs = fewer false triggers on noise.
    Lower stop_secs   = faster end-of-turn detection (but may clip mid-sentence).

    Subclassed here to log the per-chunk voice_confidence score Silero computes.
    A chunk is analyzed every ~32ms; we sample every Nth chunk plus log every
    transition that crosses the configured VAD_CONFIDENCE threshold.
    """
    class LoggingSileroVAD(SileroVADAnalyzer):
        _LOG_EVERY_N = 5  # ~6 logs/sec at 32ms chunk rate

        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self._vad_log_count = 0
            self._vad_last_above = False

        def voice_confidence(self, buffer) -> float:
            score = super().voice_confidence(buffer)
            s = float(score.item() if hasattr(score, "item") else score)
            above = s >= VAD_CONFIDENCE
            # Log only on transitions (silence → speech, speech → silence) —
            # avoids the ~6 logs/sec firehose during long utterances.
            if above != self._vad_last_above:
                logger.info("vad_transition | %s | score=%.2f | threshold=%.2f",
                            "speech" if above else "silence", s, VAD_CONFIDENCE)
            self._vad_last_above = above
            return score

    return LoggingSileroVAD(
        params=VADParams(
            confidence=VAD_CONFIDENCE,
            start_secs=VAD_MIN_SPEECH_MS / 1000.0,
            stop_secs=VAD_MIN_SILENCE_MS / 1000.0,
        )
    )


def turn_analyzer():
    """
    Smart Turn v3 — neural end-of-turn classifier that sits on top of VAD.

    VAD fires "user stopped speaking" when there's silence. Smart Turn looks
    at the transcript to decide whether the silence is a mid-sentence pause or
    a genuine end-of-turn. This reduces premature LLM triggers on slow speakers.

    CUSTOMIZE: return None to disable Smart Turn and rely purely on VAD silence.
    The caller (build_and_run) handles None gracefully.
    """
    try:
        import time
        from pipecat.audio.turn.smart_turn.local_smart_turn_v3 import (
            LocalSmartTurnAnalyzerV3,
        )

        class LoggingSmartTurn(LocalSmartTurnAnalyzerV3):
            def _predict_endpoint(self, audio_array):
                sr = self.sample_rate or 16000
                duration_s = len(audio_array) / sr
                t0 = time.perf_counter()
                result = super()._predict_endpoint(audio_array)
                elapsed_ms = (time.perf_counter() - t0) * 1000

                prob = result.get("probability", 0.0)
                original_pred = result.get("prediction", 0)
                # Override prediction with our configured threshold (model's
                # internal cutoff is 0.5). Lower threshold = fire turn sooner
                # on borderline INCOMPLETE predictions.
                overridden_pred = 1 if prob > SMART_TURN_PROB_THRESHOLD else 0
                result["prediction"] = overridden_pred

                logger.info(
                    f"""smart_turn | in: samples={len(audio_array)=} duration_s={duration_s=} sr={sr=} | 
                    out: probability={prob=} | model_pred={overridden_pred=} | threshold={elapsed_ms=} | """
                )
                return result

        return LoggingSmartTurn()
    except Exception as exc:
        logger.warning("smart-turn v3 unavailable (%s) — turn detection disabled", exc)
        return None


def build_turn_stop_strategies() -> list[BaseUserTurnStopStrategy]:
    """
    Build the list of user turn stop strategies based on TURN_DETECTION_MODE.

    Combinations (set via .env or env var):

      early_transcript  — (default) EarlyTranscriptionUserTurnStopStrategy fires the
                          moment a confident, sentence-final transcript arrives;
                          SpeechTimeout parent handles the VAD-silence fallback.

      vad_only          — SpeechTimeoutUserTurnStopStrategy only: wait
                          VAD_MIN_SILENCE_MS of silence then fire. No neural model,
                          no STT confidence gate. Good baseline for latency comparison.

      smart_turn        — TurnAnalyzerUserTurnStopStrategy wrapping the local Smart
                          Turn v3 model. Neural end-of-turn classifier runs on every
                          audio chunk; fires once the model says COMPLETE and a
                          transcript is available. Falls back to vad_only if the model
                          fails to load.

      smart_turn_early  — Smart Turn v3 + EarlyTranscription stacked. Both strategies
                          process every frame independently; whichever calls
                          trigger_user_turn_stopped() first wins. In practice:
                          confident sentence-final transcripts → EarlyTranscription
                          fires fast; hesitant / mid-sentence pauses → Smart Turn
                          catches the real end. Best of both worlds.
    """
    mode = TURN_DETECTION_MODE
    logger.info("turn_detection | mode=%s", mode)

    if mode == "vad_only":
        # With Sarvam STT, END_SPEECH is broadcast as UserStoppedSpeakingFrame
        # before TranscriptionFrame.finalized is ever set (Sarvam never sets it).
        # SarvamVADUserTurnStopStrategy intercepts that frame and cancels the
        # stt_timeout safety-net, so the turn fires on transcript arrival
        # (~252ms) instead of waiting the full P99 stt_timeout (~878ms).
        cls = (
            SarvamVADUserTurnStopStrategy if STT_PRIMARY == "sarva"
            else SpeechTimeoutUserTurnStopStrategy
        )
        return [cls(user_speech_timeout=USER_SPEECH_TIMEOUT_MS / 1000.0)]

    if mode in ("smart_turn", "smart_turn_early"):
        ta = turn_analyzer()
        if ta is not None:
            # Sarvam never sets TranscriptionFrame.finalized=True, which makes
            # the default TurnAnalyzer wait its full stt_timeout safety-net
            # (~370ms) even when Smart Turn already said COMPLETE. The Sarvam
            # subclass forces finalized=True so the turn fires immediately.
            smart_cls = (
                SarvamTurnAnalyzerUserTurnStopStrategy if STT_PRIMARY == "sarvam"
                else TurnAnalyzerUserTurnStopStrategy
            )
            smart_strategy = smart_cls(turn_analyzer=ta)
            if mode == "smart_turn":
                return [smart_strategy]
            # smart_turn_early: stack both; first to fire wins
            return [
                smart_strategy,
                EarlyTranscriptionUserTurnStopStrategy(
                    user_speech_timeout=2,
                    min_confidence=EARLY_TRIGGER_MIN_CONF,
                ),
            ]
        logger.warning(
            "turn_detection | smart_turn unavailable; falling back to early_transcript"
        )
        # fall through to early_transcript

    # Default: early_transcript
    return [
        EarlyTranscriptionUserTurnStopStrategy(
            user_speech_timeout=2,
            min_confidence=EARLY_TRIGGER_MIN_CONF,
        )
    ]


def load_lid_model(disable: bool = False):
    """
    Load the SpeechBrain voxlingua107 ECAPA-TDNN model (~200 MB download on first run).

    CUSTOMIZE: swap for a different model by changing source= and savedir=.
    Disable by passing --no-lid on the CLI (sets disable=True here).
    """
    if disable:
        return None
    try:
        from speechbrain.pretrained import EncoderClassifier
        return EncoderClassifier.from_hparams(
            source="speechbrain/lang-id-voxlingua107-ecapa",
            savedir="/tmp/speechbrain_lid",
            run_opts={"device": "cpu"},
        )
    except Exception as exc:
        logger.warning("SpeechBrain LID disabled: %s", exc)
        return None


async def build_and_run(
    transport,
    call_id: str,
    lid_model=None,
    handle_sigint: bool = True,
    memory: ConversationMemory | None = None,
    debug_frames: bool = True,
) -> None:
    # ── Shared state ─────────────────────────────────────────────────────────
    # LanguageState is a plain dataclass passed by reference to every processor
    # that needs to read or update the current language. It also holds scratch
    # space for early-barge-in concat and the idle watchdog.
    state = LanguageState(
        current_language=DEFAULT_LANGUAGE,
        call_id=call_id,
        campaign_id=STT_CAMPAIGN_ID,
        supported_languages=list(SUPPORTED_LANGUAGES),
    )

    # ── Conversation history ──────────────────────────────────────────────────
    # seed_if_empty: loads existing Redis history, or writes the system prompt
    # as the first message. Production (main.py) uses RedisMemory; dev
    # (run_webrtc.py) uses InMemoryMemory.
    if memory is None:
        memory = RedisMemory(state.call_id)
    # Two system messages: static base prompt at index 0, swappable stage
    # overlay at index 1. StageOverlayProcessor mutates messages[1] in place
    # each turn; StageRouterProcessor updates state.current_stage from the
    # LLM's trailing `[[stage:xxx]]` marker.
    history = await memory.seed_if_empty([
        {"role": "system", "content": build_base_prompt()},
        {"role": "system", "content": build_stage_prompt(INITIAL_STAGE)},
    ])
    # Restore prior stage if this is a resumed call (Redis-backed).
    try:
        state.current_stage = await memory.get_stage()
        logger.info(
            "loaded %d history messages for call %s | stage=%s",
            len(history),
            state.call_id,
            state.current_stage,
        )
    except Exception:
        state.current_stage = INITIAL_STAGE
    logger.info(
        "loaded %d history messages for call %s | stage=%s",
        len(history),
        state.call_id,
        state.current_stage,
    )

    # ── Service instantiation ─────────────────────────────────────────────────
    # All three are selected by .env vars (STT_PRIMARY, LLM_VENDOR, TTS_VENDOR).
    # See services/ and stt/ for adding new vendors.
    stt = build_stt(state)
    llm = build_llm()
    tts = build_tts(state.current_language)

    # Optional debug recorder: borrower on the left channel, bot on the right.
    recorder = StereoCallRecorder(call_id, sample_rate=SAMPLE_RATE) if CALL_RECORDING_ENABLED else None

    # SpeechBrain LID runs as a FrameProcessor before STT (optional).
    lid_proc = LIDProcessor(state, lid_model, sample_rate=SAMPLE_RATE) if lid_model else None

    # ── TTS language-switch callback ──────────────────────────────────────────
    # Called by LanguageSuffixProcessor when hysteresis commits a new language.
    # Tries both Sarvam's kwarg name (target_language_code) and the generic one
    # (language) so the same callback works across vendors.
    def on_language_switch(old: str, new: str) -> None:
        logger.info("language switch %s -> %s; updating TTS", old, new)
        iso = LANG_TO_ISO.get(new, "hi")
        # Sarvam expects a region-suffixed code (e.g. "hi-IN"); other vendors
        # take the bare ISO code.
        sarvam_code = f"{iso}-IN" if TTS_VENDOR == "sarvam" else iso
        for kw, val in (("target_language_code", sarvam_code), ("language", iso)):
            try:
                tts.update_options(**{kw: val})
                # stt._update_settings(new)
                logger.info(
                    "tts_language_updated | lang=%s | kwarg=%s | value=%s",
                    new,
                    kw,
                    val,
                )
                return
            except Exception:
                continue
        logger.warning("TTS language update: no compatible kwarg")

    # inject_suffix is True only for the internal Credgenics HTTP STT, which
    # doesn't attach language metadata to transcripts. All streaming STTs
    # (Sarvam, Deepgram) carry their own language signal so the suffix isn't
    # needed and would just pollute the LLM context.
    # inject_suffix = STT_PRIMARY in ("credgenics", "credgenics_http")
    inject_suffix = True

    # ── LLM context ───────────────────────────────────────────────────────────
    # LLMContext holds the message list. LLMContextAggregatorPair splits it into
    # a user aggregator (before LLM) and an assistant aggregator (after transport
    # output). The user aggregator OWNS VAD + turn detection + interruption:
    #   • vad_analyzer  → runs Silero on every InputAudioRawFrame it receives
    #   • user_turn_strategies → VADUserTurnStartStrategy triggers interruption
    #                            TurnAnalyzerUserTurnStopStrategy fires end-of-turn
    # Without passing vad_analyzer here, VAD never runs (TransportParams in
    # Pipecat 1.1 does NOT have vad_analyzer — it would be silently dropped).
    context = LLMContext(messages=[
        {"role": m["role"], "content": m["content"]} for m in history
    ])
    # logger.info(f"""{context.messages=}""")

    # In Pipecat 1.1, VAD + Smart Turn are wired through the user aggregator,
    # NOT through TransportParams (those fields don't exist there and get
    # silently dropped). Without this wiring, VADUserStartedSpeakingFrames
    # are never emitted, the user-turn-started event never fires, and the
    # LLM aggregator never broadcasts an InterruptionFrame — i.e. barge-in
    # appears to "happen after the bot finishes" because nothing actually
    # interrupts the in-flight TTS.
    # turn = turn_analyzer()
    # turn = None  # A/B: smart turn disabled
    # if turn is not None:
    #     stop_strategies = [TurnAnalyzerUserTurnStopStrategy(turn_analyzer=turn)]
    # else:
    #     # Pure-VAD path: passing stop=None would silently re-instantiate
    #     # LocalSmartTurnAnalyzerV3 via default_user_turn_stop_strategies().
    #     # SpeechTimeoutUserTurnStopStrategy waits user_speech_timeout after
    #     # VADUserStoppedSpeakingFrame (gated on at least one transcript) —
    #     # no neural classifier, no multi-second confirmation window.
    #     stop_strategies = [SpeechTimeoutUserTurnStopStrategy(user_speech_timeout=0.01)]
    # Non-streaming Credgenics STT doesn't emit per-transcript confidence, so the
    # EarlyTranscription / SmartTurn strategies can't gate on it. Use the
    # transcript-aware SpeechTimeout strategy instead: it fires once this STT's
    # finalized transcript arrives after VAD stop (plus a short floor).
    if STT_PRIMARY in ("credgenics", "credgenics_http"):
        stop_strategies = [
            SpeechTimeoutUserTurnStopStrategy(user_speech_timeout=USER_SPEECH_TIMEOUT_MS / 1000.0)
        ]
    else:
        stop_strategies = build_turn_stop_strategies()
    user_turn_strategies = UserTurnStrategies(
        start=default_user_turn_start_strategies(),
        stop=stop_strategies,
    )
    user_params = LLMUserAggregatorParams(
        vad_analyzer=vad_analyzer(),
        user_turn_strategies=user_turn_strategies,
        # Pipecat default is 5.0s. When Smart Turn fires COMPLETE but no STT
        # transcript arrives (e.g. silent barge-in, STT stream reset after
        # interruption), the stop strategies deadlock until this timeout forces
        # UserStoppedSpeakingFrame. Shorter = less perceived silence on those turns.
        user_turn_stop_timeout=1.5,
    )
    context_aggr = LLMContextAggregatorPair(context, user_params=user_params)

    # Debug-only: instantiate to time frames across context_aggr.user(). Drop
    # `.pre()` / `.post()` around it in the procs list when investigating
    # aggregator latency.
    _aggr_user_probe = AggregatorLatencyProbe("context_aggr_user")

    # ── Pipeline assembly ─────────────────────────────────────────────────────
    # Processors are chained in order. Frames flow downstream unless a processor
    # calls broadcast_frame() or push_frame(..., UPSTREAM).
    #
    # TO ADD A STEP: instantiate your FrameProcessor and insert it at the right
    # position below. Common insertion points are marked with comments.
    procs = [transport.input(), EventLogger("input")]
    if recorder is not None:
        # Capture borrower audio straight off the wire (left channel).
        procs.insert(1, recorder.user_tap)

    # if debug_frames:
    #     # FrameTap logs every non-audio frame name at this pipeline stage.
    #     # Enable with: python -m voicebot.run_webrtc --debug-frames
    #     procs.append(FrameTap("input", include_audio=True))

    if lid_proc is not None:
        # LID sees raw audio BEFORE STT so it doesn't block the transcript.
        # It updates LanguageState.candidate_language as a side-effect.
        procs.append(lid_proc)

    # if debug_frames:
    #     procs.append(FrameTap("pre-stt"))

    procs.append(stt)
    # Note: TranscriptionFrame.language may carry a region suffix from some
    # vendors (e.g. Sarvam returns "hi-IN"). LanguageSuffixProcessor below
    # strips the suffix when voting into LanguageState.current_language, so
    # the rest of the pipeline reads bare ISO codes via state — no separate
    # normalizer processor is needed.
    # ← GOOD INSERTION POINT: post-STT transcript manipulation
    # (e.g. profanity filter, custom LID fallback)

    # if debug_frames:
    #     procs.append(FrameTap("post-stt"))

    procs.extend([
        # EarlyBargeInConcatProcessor is disabled by default.
        # Enable it to stash the previous transcript when the user interrupts
        # within EARLY_BARGE_IN_WINDOW_S and prepend it to the next utterance.
        # Requires RedisMemory (won't crash with InMemoryMemory but trim_last
        # is a no-op on in-memory). Re-enable here when barge-in concat is needed:
        # EarlyBargeInConcatProcessor(state, memory),

        # Commits hysteresis-gated language switches; appends per-language suffix
        # to TranscriptionFrame.text when inject_suffix=True (Credgenics STT only).
        LanguageSuffixProcessor(state, on_language_switch=on_language_switch,
                                inject_suffix=inject_suffix),

        # Appends user transcript to Redis immediately after the suffix is injected.
        RedisUserRecorder(memory),

        # User-side LLM aggregator: collects transcript, manages LLMContext,
        # fires the LLM on UserStoppedSpeakingFrame. VAD runs inside here.
        # Re-wrap with `_aggr_user_probe.pre()` / `.post()` to debug latency
        # through this stage (see processors/aggregator_probe.py).
        context_aggr.user(),

        # Fires a short language-matched TTS phrase ("हाँ", "okay", "एक सेकंड")
        # the instant UserStoppedSpeakingFrame fires — masks LLM TTFB so the
        # caller hears something within ~150ms of finishing their turn. The
        # filler bypasses LLM and LLMContext; the LLM response queues behind
        # it in TTS and plays seamlessly after.
        # FillerInjectorProcessor(state),

        # Swaps LLMContext.messages[1] to the current-stage overlay just
        # before each LLM call. Must sit AFTER context_aggr.user() (so the
        # user turn is already appended) and BEFORE the LLM.
        StageOverlayProcessor(state, context),
    ])

    # if debug_frames:
    #     procs.append(FrameTap("post-context-aggr"))

            
    _latency_shared = _LatencyShared()
    procs.extend([llm,
        # Captures true LLM stream start/end BEFORE TTS holds the End frame
        # as a flush signal. Shares timestamps with TurnLatencyTracker below.
        LLMTimingProbe(_latency_shared),
        # Strips the trailing `[[stage:xxx]]` marker before downstream
        # processors / TTS see it, and commits the parsed stage to state +
        # memory on LLMFullResponseEndFrame.
        StageRouterProcessor(state, memory),
        # ← GOOD INSERTION POINT: post-LLM text processing
        # (e.g. response filter, SSML injection, language-specific post-processing)
        TextNormalizationProcessor(lang="en", state=state)
    ])

    # if debug_frames:
    #     procs.append(FrameTap("post-llm"))

    procs.extend([
        # Accumulates LLM TextFrames into a full response, then appends to Redis.
        # RedisAssistantRecorder(memory),

        # Uninterruptible-greeting gate: while state.greeting_active is True,
        # swallows InterruptionFrame / UserStartedSpeakingFrame so the deterministic
        # first message can't be cancelled by VAD false-triggers. Bot keeps
        # listening — STT and aggregator are upstream and run normally.
        GreetingGate(state),

        # Converts streaming TextFrames → OutputAudioRawFrames via the TTS vendor.
        tts,
        # ← GOOD INSERTION POINT: post-TTS audio processing
        # (e.g. audio normalization, logging playback duration)
    ])

    if recorder is not None:
        # Capture synthesized bot audio right after TTS (right channel).
        procs.insert(procs.index(tts) + 1, recorder.bot_tap)

    # if debug_frames:
    #     procs.append(FrameTap("post-tts", include_audio=True))

    procs.extend([
        # If the LLM turn errors / ends empty, or the STT reports an unsupported
        # language, play a localized recovery prompt instead of going silent.
        RepeatPromptOnFailure(state),

        # Idle watchdog: emits ARE_YOU_THERE_TEXT after ARE_YOU_THERE_TIMEOUT_S
        # of silence; hangs up after ARE_YOU_THERE_MAX_STRIKES strikes.
        AreYouThereWatchdog(state),

        # End-of-call trigger: when TextNormalizationProcessor stripped a
        # "| END |" marker from the LLM response, state.end_after_speech is
        # True. This processor watches for BotStoppedSpeakingFrame (emitted
        # by TTS after the goodbye finishes playing) and fires the hangup
        # hook + EndFrame.
        EndCallTrigger(state),

        # Per-turn latency summary: logs one TURN_LATENCY line per turn with
        # vad+turn / stt / llm_ttfb / llm_total / tts_ttfb / bot_start / TOTAL.
        TurnLatencyTracker(_latency_shared),

        # EventLogger("output"),
        transport.output(),

        # Flips state.greeting_active to False on first BotStoppedSpeakingFrame —
        # restoring normal barge-in behaviour for the rest of the call.
        GreetingDoneFlag(state),

        # Assistant-side aggregator: commits the full assistant response into
        # LLMContext AFTER it has been played out. Placed after transport.output()
        # so context is only updated on successfully delivered responses.
        context_aggr.assistant(),
    ])

    pipeline = Pipeline(procs)

    # ── PipelineTask ──────────────────────────────────────────────────────────
    # allow_interruptions=True: when VADUserStartedSpeakingFrame fires while the
    # bot is speaking, context_aggr.user() calls broadcast_interruption() which
    # sends InterruptionFrame through the whole pipeline, causing the output
    # transport to discard buffered audio and the TTS to cancel in-flight synthesis.
    task = PipelineTask(
        pipeline,
        params=PipelineParams(
            # allow_interruptions=True,
            enable_metrics=True,
            enable_usage_metrics=True,
            audio_in_sample_rate=SAMPLE_RATE,
            audio_out_sample_rate=SAMPLE_RATE,
        ),
    )

    # ── Deterministic first message ──────────────────────────────────────────
    # Pulled from pd_si.py's first_message dict (language + gender variant),
    # templated with call_data, sent straight to TTS via TTSSpeakFrame so it
    # bypasses the LLM entirely. GreetingGate keeps it uninterruptible until
    # the bot finishes speaking.
    greeting = build_first_message()
    await task.queue_frames([TTSSpeakFrame(greeting, append_to_context=True)])
    # Persist as the first assistant turn so the LLM context reflects what
    # was actually said, and the LLM doesn't repeat the introduction.
    await memory.append("assistant", greeting)
    logger.info("first_message | text=%r", greeting)

    runner = PipelineRunner(handle_sigint=handle_sigint)
    try:
        await runner.run(task)
    finally:
        if recorder is not None:
            recorder.finalize()
        await memory.close()