"""
Shared pipeline assembly — used by both the production (Asterisk RTP) and
the dev (WebRTC) runners. Anything transport-agnostic lives here.
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
    VAD_CONFIDENCE,
    VAD_MIN_SILENCE_MS,
    VAD_MIN_SPEECH_MS,
)
from voicebot.processors.are_you_there import AreYouThereWatchdog
from voicebot.processors.early_barge_in import EarlyBargeInConcatProcessor
from voicebot.processors.event_logger import EventLogger
from voicebot.processors.frame_tap import FrameTap
from voicebot.processors.language_suffix import LanguageSuffixProcessor
from voicebot.processors.redis_recorder import RedisAssistantRecorder, RedisUserRecorder
from voicebot.prompts.call_data import build_system_prompt
from voicebot.services.llm_factory import build_llm
from voicebot.services.tts_factory import build_tts
from voicebot.state.language_state import LanguageState
from voicebot.state.memory import ConversationMemory
from voicebot.state.redis_memory import RedisMemory
from voicebot.stt.factory import build_stt
from voicebot.stt.lid import LIDProcessor

logger = logging.getLogger("voicebot")


def vad_analyzer() -> SileroVADAnalyzer:
    return SileroVADAnalyzer(
        params=VADParams(
            confidence=VAD_CONFIDENCE,
            start_secs=VAD_MIN_SPEECH_MS / 1000.0,
            stop_secs=VAD_MIN_SILENCE_MS / 1000.0,
        )
    )


def turn_analyzer():
    try:
        from pipecat.audio.turn.smart_turn.local_smart_turn_v3 import (
            LocalSmartTurnAnalyzerV3,
        )
        return LocalSmartTurnAnalyzerV3()
    except Exception as exc:
        logger.warning("smart-turn v3 unavailable (%s) — turn detection disabled", exc)
        return None


def load_lid_model(disable: bool = False):
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
    state = LanguageState(
        current_language=DEFAULT_LANGUAGE,
        call_id=call_id,
        supported_languages=list(SUPPORTED_LANGUAGES),
    )

    if memory is None:
        memory = RedisMemory(state.call_id)
    history = await memory.seed_if_empty([
        {"role": "system", "content": build_system_prompt()},
    ])
    logger.info("loaded %d history messages for call %s", len(history), state.call_id)

    stt = build_stt(state)
    llm = build_llm()
    tts = build_tts(state.current_language)
    lid_proc = LIDProcessor(state, lid_model, sample_rate=SAMPLE_RATE) if lid_model else None

    def on_language_switch(old: str, new: str) -> None:
        logger.info("language switch %s -> %s; updating TTS", old, new)
        iso = LANG_TO_ISO.get(new, "hi")
        for kw, val in (("target_language_code", f"{iso}-IN"), ("language", iso)):
            try:
                tts.update_options(**{kw: val})
                return
            except Exception:
                continue
        logger.warning("TTS language update: no compatible kwarg")

    inject_suffix = STT_PRIMARY in ("credgenics", "credgenics_http")

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

    procs = [transport.input(), EventLogger("input")]
    if debug_frames:
        procs.append(FrameTap("input", include_audio=True))
    if lid_proc is not None:
        procs.append(lid_proc)
    procs.append(stt)
    if debug_frames:
        procs.append(FrameTap("post-stt"))
    procs.extend([
        # EarlyBargeInConcatProcessor(state, memory),
        LanguageSuffixProcessor(state, on_language_switch=on_language_switch,
                                inject_suffix=inject_suffix),
        RedisUserRecorder(memory),
        context_aggr.user(),
        llm,
    ])
    if debug_frames:
        procs.append(FrameTap("post-llm"))
    procs.extend([
        RedisAssistantRecorder(memory),
        tts,
    ])
    if debug_frames:
        procs.append(FrameTap("post-tts", include_audio=True))
    procs.extend([
        AreYouThereWatchdog(state),
        EventLogger("output"),
        transport.output(),
        context_aggr.assistant(),
    ])
    pipeline = Pipeline(procs)

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

    runner = PipelineRunner(handle_sigint=handle_sigint)
    try:
        await runner.run(task)
    finally:
        await memory.close()
