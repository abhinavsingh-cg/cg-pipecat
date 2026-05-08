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
from pipecat.turns.user_stop import TurnAnalyzerUserTurnStopStrategy
from pipecat.turns.user_turn_strategies import (
    UserTurnStrategies,
    default_user_turn_start_strategies,
)

from voicebot.config import (
    DEFAULT_LANGUAGE,
    LANG_TO_ISO,
    SAMPLE_RATE,
    STT_PRIMARY,
    SUPPORTED_LANGUAGES,
    TTS_VENDOR,
    VAD_CONFIDENCE,
    VAD_MIN_SILENCE_MS,
    VAD_MIN_SPEECH_MS,
)
from voicebot.processors.are_you_there import AreYouThereWatchdog
from voicebot.processors.early_barge_in import EarlyBargeInConcatProcessor
from voicebot.processors.end_call import EndCallTrigger
from voicebot.processors.event_logger import EventLogger
from voicebot.processors.frame_tap import FrameTap
from voicebot.processors.greeting_gate import GreetingDoneFlag, GreetingGate
from voicebot.processors.language_suffix import LanguageSuffixProcessor
from voicebot.processors.redis_recorder import RedisAssistantRecorder, RedisUserRecorder
from voicebot.processors.text_normalizer import TextNormalizationProcessor
from voicebot.digit_handler import DigitHandlingProcessor
from voicebot.prompts.call_data import build_first_message, build_system_prompt
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
    """
    return SileroVADAnalyzer(
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
        from pipecat.audio.turn.smart_turn.local_smart_turn_v3 import (
            LocalSmartTurnAnalyzerV3,
        )
        return LocalSmartTurnAnalyzerV3()
    except Exception as exc:
        logger.warning("smart-turn v3 unavailable (%s) — turn detection disabled", exc)
        return None


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
    debug_frames: bool = False,
) -> None:
    # ── Shared state ─────────────────────────────────────────────────────────
    # LanguageState is a plain dataclass passed by reference to every processor
    # that needs to read or update the current language. It also holds scratch
    # space for early-barge-in concat and the idle watchdog.
    state = LanguageState(
        current_language=DEFAULT_LANGUAGE,
        call_id=call_id,
        supported_languages=list(SUPPORTED_LANGUAGES),
    )

    # ── Conversation history ──────────────────────────────────────────────────
    # seed_if_empty: loads existing Redis history, or writes the system prompt
    # as the first message. Production (main.py) uses RedisMemory; dev
    # (run_webrtc.py) uses InMemoryMemory.
    if memory is None:
        memory = RedisMemory(state.call_id)
    history = await memory.seed_if_empty([
        {"role": "system", "content": build_system_prompt()},
    ])
    logger.info("loaded %d history messages for call %s", len(history), state.call_id)

    # ── Service instantiation ─────────────────────────────────────────────────
    # All three are selected by .env vars (STT_PRIMARY, LLM_VENDOR, TTS_VENDOR).
    # See services/ and stt/ for adding new vendors.
    stt = build_stt(state)
    llm = build_llm()
    tts = build_tts(state.current_language)

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

    # In Pipecat 1.1, VAD + Smart Turn are wired through the user aggregator,
    # NOT through TransportParams (those fields don't exist there and get
    # silently dropped). Without this wiring, VADUserStartedSpeakingFrames
    # are never emitted, the user-turn-started event never fires, and the
    # LLM aggregator never broadcasts an InterruptionFrame — i.e. barge-in
    # appears to "happen after the bot finishes" because nothing actually
    # interrupts the in-flight TTS.
    turn = turn_analyzer()
    stop_strategies = None
    if turn is not None:
        stop_strategies = [TurnAnalyzerUserTurnStopStrategy(turn_analyzer=turn)]
    user_turn_strategies = UserTurnStrategies(
        start=default_user_turn_start_strategies(),
        stop=stop_strategies,
    )
    user_params = LLMUserAggregatorParams(
        vad_analyzer=vad_analyzer(),
        user_turn_strategies=user_turn_strategies,
    )
    context_aggr = LLMContextAggregatorPair(context, user_params=user_params)

    # ── Pipeline assembly ─────────────────────────────────────────────────────
    # Processors are chained in order. Frames flow downstream unless a processor
    # calls broadcast_frame() or push_frame(..., UPSTREAM).
    #
    # TO ADD A STEP: instantiate your FrameProcessor and insert it at the right
    # position below. Common insertion points are marked with comments.
    procs = [transport.input(), EventLogger("input")]

    if debug_frames:
        # FrameTap logs every non-audio frame name at this pipeline stage.
        # Enable with: python -m voicebot.run_webrtc --debug-frames
        procs.append(FrameTap("input", include_audio=True))

    if lid_proc is not None:
        # LID sees raw audio BEFORE STT so it doesn't block the transcript.
        # It updates LanguageState.candidate_language as a side-effect.
        procs.append(lid_proc)

    procs.append(stt)
    # Note: TranscriptionFrame.language may carry a region suffix from some
    # vendors (e.g. Sarvam returns "hi-IN"). LanguageSuffixProcessor below
    # strips the suffix when voting into LanguageState.current_language, so
    # the rest of the pipeline reads bare ISO codes via state — no separate
    # normalizer processor is needed.
    # ← GOOD INSERTION POINT: post-STT transcript manipulation
    # (e.g. profanity filter, custom LID fallback)

    if debug_frames:
        procs.append(FrameTap("post-stt"))

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
        context_aggr.user(),
        llm,
        # ← GOOD INSERTION POINT: post-LLM text processing
        # (e.g. response filter, SSML injection, language-specific post-processing)
        DigitHandlingProcessor(),
        TextNormalizationProcessor(lang="en", state=state)
    ])

    if debug_frames:
        procs.append(FrameTap("post-llm"))

    procs.extend([
        # Accumulates LLM TextFrames into a full response, then appends to Redis.
        RedisAssistantRecorder(memory),

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

    if debug_frames:
        procs.append(FrameTap("post-tts", include_audio=True))

    procs.extend([
        # Idle watchdog: emits ARE_YOU_THERE_TEXT after ARE_YOU_THERE_TIMEOUT_S
        # of silence; hangs up after ARE_YOU_THERE_MAX_STRIKES strikes.
        AreYouThereWatchdog(state),

        # End-of-call trigger: when TextNormalizationProcessor stripped a
        # "| END |" marker from the LLM response, state.end_after_speech is
        # True. This processor watches for BotStoppedSpeakingFrame (emitted
        # by TTS after the goodbye finishes playing) and fires the hangup
        # hook + EndFrame.
        EndCallTrigger(state),

        EventLogger("output"),
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
            allow_interruptions=True,
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
    await task.queue_frames([TTSSpeakFrame(greeting)])
    # Persist as the first assistant turn so the LLM context reflects what
    # was actually said, and the LLM doesn't repeat the introduction.
    await memory.append("assistant", greeting)
    logger.info("first_message | text=%r", greeting)

    runner = PipelineRunner(handle_sigint=handle_sigint)
    try:
        await runner.run(task)
    finally:
        await memory.close()
