# voicebot — Pipecat orchestrator

Migration target for `/Users/admin/credgenics-voice-bot-library` (custom RTP/SIP voice bot, ~2,800-line `RTPProcessor`). Earlier `/Users/admin/livekit/agent.py` was a partial LiveKit-Agents 1.5.2 attempt; this `voicebot/` package replaces both with Pipecat 1.1.0.

## Why Pipecat (not LiveKit Agents)

Asterisk already terminates SIP and produces μ-law/8 kHz/20 ms RTP. LiveKit SIP would have been a redundant proxy hop. Pipecat is also more explicit about pipeline composition, which fits the existing codebase's preference for direct control. Sarvam STT/TTS/LLM, AWS Bedrock LLM, Deepgram STT, ElevenLabs/Cartesia TTS, Groq/OpenAI LLM, Silero VAD, and a multilingual end-of-turn classifier (Smart Turn v3) all ship as stock Pipecat services — no custom websocket code needed.

## Layout

```
voicebot/
  main.py                  Asterisk RTP runner (production)
  run_webrtc.py            Local browser-based dev runner (FastAPI signaling)
  pipeline.py              Shared pipeline assembly used by both runners
  config.py                Env vars + language maps
  .env.example / .env      Component selectors + API keys

  transport/
    rtp_codec.py           μ-law ↔ PCM, RTP framing (ported from rtp_utils.py)
    asterisk_rtp.py        UDP-based Pipecat BaseTransport, symmetric-RTP NAT auto-learn

  stt/
    factory.py             Selector: sarvam | deepgram | credgenics_http
    credgenics_http.py     Internal /transcribe POST as a Pipecat STTService
    lid.py                 SpeechBrain LID as a side-channel FrameProcessor

  state/
    language_state.py      Hysteresis + LID + concat scratch + idle counter
    redis_memory.py        cg_redis-style call:{call_id}:data, append/load/trim_last

  processors/
    early_barge_in.py      ≤800 ms interruption stash + Redis trim + prepend on next turn
    language_suffix.py     Hysteresis commit + TTS language update; suffix injection
                           gated on inject_suffix (true only for in-house STT);
                           short-utterance guard (≤3 words → skip LID, keep current language)
    text_normalizer.py     Replaces digit sequences with words (num2words) before LLM context;
                           logs raw transcript, stt_lang, current_lang, normalized text at INFO
    are_you_there.py       Idle watchdog with 3-strike EndFrame
    redis_recorder.py      User + assistant Redis writers

  services/
    llm_factory.py         groq | openai | bedrock | sarvam (all stock Pipecat)
    tts_factory.py         sarvam | elevenlabs | cartesia (all stock Pipecat)

  prompts/
    pd_si.py               System prompt (copied from livekit/pd_si.py)
    call_data.py           Templates pd_si with call metadata

  observability.py         JSON logging + optional OTEL OTLP exporter
```

## Custom code (everything else is stock Pipecat)

| File | Why custom |
|---|---|
| `transport/asterisk_rtp.py` | Pipecat ships no raw UDP/RTP transport |
| `transport/rtp_codec.py` | Ported byte-for-byte from `rtp_utils.py` |
| `stt/credgenics_http.py` | Internal vendor (POST `/transcribe` form-encoded) |
| `stt/lid.py` | SpeechBrain side-channel — replaces the monkey-patched `LIDAwareSTT` from livekit/ |
| `state/redis_memory.py` | Preserves the `call:{call_id}:data` JSON schema |
| `processors/early_barge_in.py` | ≤800 ms concat hook (ported from `rtp_processor.py:1015-1019, 2690-2703, 1304-1319`) |
| `processors/language_suffix.py` | Hysteresis + TTS language update; ≤3-word short-utterance guard |
| `processors/text_normalizer.py` | Digit-to-word normalization (num2words) + transcript INFO logging |
| `processors/are_you_there.py` | Idle prompts ported from `rtp_processor.py:2572+` |
| `processors/redis_recorder.py` | Application schema |

## Discarded outright

- `cg_voicebot/rtp_processor.py` — entire ~2,800-line RTPProcessor + Voice/Silence state machine.
- `cg_voicebot/stt_vendor.py:SarvamSTTConnection` (118-485) → stock `pipecat.services.sarvam.stt`.
- `cg_voicebot/stt_vendor.py:SarvamTTSConnection` (260-485), including the 15-frame TTS pre-buffer hack → stock `pipecat.services.sarvam.tts`.
- `cg_voicebot/llm_vendor.py:LLMVendorFactory` + 4 vendor classes (line 349) → stock Pipecat LLM services.
- Local Silero VAD via ONNX (`voice_bot.py:171`), `process_vad`, `vad_buffer`, RMS gating, `PRE_BUFFER_PACKETS` → `SileroVADAnalyzer` + Smart Turn v3.
- Pydantic `model_rebuild` workaround (livekit/agent.py:32-87) — not needed.
- LiveKit `agent.py`, `lid_stt.py` monkey-patch — superseded.

## Pipecat 1.1.0 API tweaks (vs. plan written against earlier docs)

The plan assumed module names that have since moved:

- `OpenAILLMContext` → `from pipecat.processors.aggregators.llm_context import LLMContext` plus `LLMContextAggregatorPair(context)` from `pipecat.processors.aggregators.llm_response_universal` (LLMService no longer has `create_context_aggregator`).
- `StartInterruptionFrame` / `StopInterruptionFrame` → `InterruptionFrame`.
- `SmallWebRTCConnection.handle_client_offer(...)` → `await connection.initialize(sdp, type)` then **synchronous** `connection.get_answer()` (returns a dict, not a coroutine).
- Sarvam STT/TTS now take `params=SarvamSTTService.InputParams(...)` / `params=SarvamTTSService.InputParams(...)` for language/pace/temperature; TTS uses `voice_id`, not `speaker`.
- `pipecat-ai[smart-turn]` extra is gone — `LocalSmartTurnAnalyzerV3` is shipped in the base install.

## Environment + run instructions

```
conda create -y -n pipecat-voicebot python=3.11
conda activate pipecat-voicebot
pip install "pipecat-ai[silero,sarvam,groq,webrtc]" \
    fastapi "uvicorn[standard]" python-json-logger redis aiohttp \
    langdetect python-dotenv torch torchaudio onnxruntime num2words
```

For other vendors add the matching extras (`deepgram`, `elevenlabs`, `cartesia`, `aws`, `openai`).

```
cp voicebot/.env.example voicebot/.env
# edit .env: STT_PRIMARY, LLM_VENDOR, TTS_VENDOR, API keys

# Production (Asterisk):
python -m voicebot.main --local-port 4000 --remote 10.0.0.5:40000 --call-id abc

# Local browser smoke test:
python -m voicebot.run_webrtc --no-lid
# → open http://127.0.0.1:7860 in Chrome → "Start call"
```

`--no-lid` skips the ~200 MB SpeechBrain download. Drop it once language detection is needed.

## Status / known caveats

- ✅ Conda env `pipecat-voicebot` ready; package compiles and imports.
- ✅ `python -m voicebot.run_webrtc` boots, serves the dev HTML at `/`.
- ✅ `/api/offer` accepts an SDP offer and returns an answer (after the `await connection.get_answer()` → `connection.get_answer()` fix).
- ⏳ End-to-end audio loop (mic → STT → LLM → TTS → speaker) not yet verified — next step is to click Start in Chrome and iron out whatever frame-flow issues come up.
- ⏳ Frame names for early-barge-in (`BotStartedSpeakingFrame`, `LLMFullResponseStartFrame`, `InterruptionFrame`) are believed correct for Pipecat 1.1 but the exact firing order on a real turn hasn't been observed. The processor is defensive — wrong-order or missing frames mean concat just doesn't trigger.
- ⏳ Redis is required only for `python -m voicebot.main` (production). `run_webrtc` uses `state/memory.InMemoryMemory` — no Redis needed for local browser smoke testing.
- ⏳ Asterisk RTP transport (`voicebot/transport/asterisk_rtp.py`) hasn't been exercised against real Asterisk yet — auto-learned remote endpoint and symmetric-RTP behavior need a live test.
- ⏳ Tests deferred per user direction (smoke test via WebRTC instead).

## Migration plan reference

Full plan: `/Users/admin/.claude/plans/we-want-to-migrate-compiled-dragonfly.md`.
