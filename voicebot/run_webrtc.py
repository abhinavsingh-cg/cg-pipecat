"""
voicebot.run_webrtc — local browser-based smoke test.

Uses Pipecat's stock SmallWebRTCRequestHandler to manage the offer/answer
dance. Each browser connection spawns a fresh pipeline backed by
SmallWebRTCTransport, with InMemoryMemory (no Redis required for dev).

  python -m voicebot.run_webrtc
  python -m voicebot.run_webrtc --no-lid
  python -m voicebot.run_webrtc --port 8080
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys

import uvicorn
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse

from voicebot.config import SAMPLE_RATE, WEBRTC_HOST, WEBRTC_PORT
from voicebot.observability import setup_logging
from voicebot.pipeline import build_and_run, load_lid_model
from voicebot.state.memory import InMemoryMemory

logger = logging.getLogger("voicebot.webrtc")

_HTML = """\
<!doctype html>
<html><head><meta charset="utf-8"><title>voicebot dev</title></head>
<body style="font-family:sans-serif;max-width:640px;margin:2em auto;">
<h2>voicebot WebRTC dev</h2>
<button id="start">Start call</button>
<button id="stop" disabled>Stop</button>
<pre id="log" style="background:#111;color:#0f0;padding:1em;height:240px;overflow:auto;"></pre>
<audio id="audio" autoplay></audio>
<script>
const log = m => { const el=document.getElementById('log'); el.textContent += m+"\\n"; el.scrollTop=el.scrollHeight; };
let pc, stream;
document.getElementById('start').onclick = async () => {
  document.getElementById('start').disabled = true;
  document.getElementById('stop').disabled = false;
  stream = await navigator.mediaDevices.getUserMedia({ audio:true, video:false });
  pc = new RTCPeerConnection();
  pc.ontrack = e => { document.getElementById('audio').srcObject = e.streams[0]; log('remote track'); };
  pc.oniceconnectionstatechange = () => log('ice: ' + pc.iceConnectionState);
  pc.onconnectionstatechange = () => log('pc: ' + pc.connectionState);
  // Audio-only call — but request a recvonly transceiver so the bot's track binds.
  pc.addTransceiver('audio', { direction: 'sendrecv' });
  stream.getTracks().forEach(t => pc.addTrack(t, stream));
  const offer = await pc.createOffer();
  await pc.setLocalDescription(offer);
  log('posting offer...');
  const r = await fetch('/api/offer', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({ sdp: pc.localDescription.sdp, type: pc.localDescription.type })});
  const ans = await r.json();
  log('got answer (pc_id='+(ans.pc_id||'')+')');
  await pc.setRemoteDescription(ans);
};
document.getElementById('stop').onclick = () => {
  if (pc) pc.close();
  if (stream) stream.getTracks().forEach(t=>t.stop());
  document.getElementById('start').disabled = false;
  document.getElementById('stop').disabled = true;
  log('stopped');
};
</script>
</body></html>
"""


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="voicebot — local WebRTC dev runner")
    p.add_argument("--host", default=WEBRTC_HOST)
    p.add_argument("--port", type=int, default=WEBRTC_PORT)
    p.add_argument("--no-lid", action="store_true", help="skip SpeechBrain LID load")
    p.add_argument("--debug-frames", action="store_true",
                   help="log every non-audio frame flowing through the pipeline")
    return p.parse_args(argv)


def _print_startup_banner(logger: logging.Logger) -> None:
    from voicebot.config import (
        DEEPGRAM_API_KEY, GROQ_API_KEY, LLM_VENDOR, OPENAI_API_KEY,
        SARVAM_API_KEY, STT_PRIMARY, TTS_VENDOR,
    )
    keys = {
        "stt:sarvam": bool(SARVAM_API_KEY) if STT_PRIMARY == "sarvam" else None,
        "stt:deepgram": bool(DEEPGRAM_API_KEY) if STT_PRIMARY == "deepgram" else None,
        "llm:groq": bool(GROQ_API_KEY) if LLM_VENDOR == "groq" else None,
        "llm:openai": bool(OPENAI_API_KEY) if LLM_VENDOR == "openai" else None,
        "tts:sarvam": bool(SARVAM_API_KEY) if TTS_VENDOR == "sarvam" else None,
    }
    missing = [k for k, v in keys.items() if v is False]
    logger.info("vendors: STT=%s LLM=%s TTS=%s", STT_PRIMARY, LLM_VENDOR, TTS_VENDOR)
    if missing:
        logger.warning("missing API keys: %s — pipeline will fail at first turn", missing)


def build_app(lid_model, debug_frames: bool = False) -> FastAPI:
    """Per-connection: build a fresh SmallWebRTCTransport + pipeline."""
    from pipecat.transports.base_transport import TransportParams                       # type: ignore
    from pipecat.transports.smallwebrtc.request_handler import (                         # type: ignore
        SmallWebRTCRequest,
        SmallWebRTCRequestHandler,
    )
    from pipecat.transports.smallwebrtc.transport import SmallWebRTCTransport            # type: ignore

    handler = SmallWebRTCRequestHandler()
    app = FastAPI(title="voicebot dev")

    @app.get("/", response_class=HTMLResponse)
    async def index() -> HTMLResponse:
        return HTMLResponse(_HTML)

    async def on_connection(connection) -> None:
        """
        Called by SmallWebRTCRequestHandler after `initialize()` has set up
        the peer connection. Bind transport + pipeline to it now (so the
        audio track is wired before frames start flowing). Run the pipeline
        as a background task — we must return promptly so the handler can
        produce the SDP answer for the browser.
        """
        # NOTE: TransportParams in Pipecat 1.1 does NOT accept vad_analyzer
        # or turn_analyzer — those are wired into the user-side LLM
        # aggregator inside build_and_run. Passing them here would be
        # silently dropped by Pydantic and break interruption.
        params = TransportParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            audio_in_sample_rate=SAMPLE_RATE,
            audio_out_sample_rate=SAMPLE_RATE,
        )
        transport = SmallWebRTCTransport(connection, params=params)
        call_id = f"webrtc-{connection.pc_id}"

        async def _run():
            try:
                await build_and_run(
                    transport, call_id, lid_model,
                    handle_sigint=False,
                    memory=InMemoryMemory(call_id),
                    debug_frames=debug_frames,
                )
            except Exception:
                logger.exception("pipeline crashed")

        asyncio.create_task(_run())

    @app.post("/api/offer")
    async def offer(payload: dict) -> JSONResponse:
        request = SmallWebRTCRequest.from_dict(dict(payload))
        answer = await handler.handle_web_request(request, on_connection)
        return JSONResponse(answer)

    return app


def main(argv: list[str]) -> None:
    setup_logging()
    args = parse_args(argv)
    _print_startup_banner(logger)
    lid_model = load_lid_model(args.no_lid)
    app = build_app(lid_model, debug_frames=args.debug_frames)
    logger.info("WebRTC dev server: http://%s:%d", args.host, args.port)
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main(sys.argv[1:])
