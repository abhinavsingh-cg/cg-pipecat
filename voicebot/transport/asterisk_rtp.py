"""
AsteriskRTPTransport — Pipecat transport over raw UDP/RTP.

WHY CUSTOM:
  Asterisk terminates SIP and produces μ-law @ 8 kHz / 20 ms RTP. We bind
  a UDP port and exchange RTP directly — no LiveKit SIP / Daily / Twilio hop.
  Pipecat ships no raw UDP transport so this is custom.

SYMMETRIC RTP (NAT traversal):
  We don't know Asterisk's public IP until it sends us a packet. The input
  transport auto-learns the remote {ip:port} from the first inbound datagram
  (_on_remote_learned). The output transport reuses the same socket and sends
  to that learned address. This is how symmetric RTP NAT traversal works.

FRAME FLOW:
  AsteriskRTPInputTransport:
    UDP datagram → parse_rtp_header() → decode_ulaw_to_pcm()
    → InputAudioRawFrame(audio=pcm_bytes, sample_rate=8000) → pipeline

  AsteriskRTPOutputTransport:
    OutputAudioRawFrame → (resample if needed) → encode_pcm_to_ulaw()
    → prepare_ulaw_stream_frames() → build_rtp_packet() → UDP sendto()

CUSTOMIZE:
  • Change RTP payload type: edit build_rtp_packet() in rtp_codec.py (byte [1]).
    Currently hardcoded to 0x00 (PCMU / μ-law).
  • Change SSRC: edit config.py SSRC (arbitrary fixed value, Asterisk ignores it).
  • Pre-announced remote: pass remote_host + remote_port to AsteriskRTPParams
    if you know Asterisk's address at startup (skips symmetric-RTP auto-learn).
  • Jitter buffer: not implemented — if Asterisk jitter is a problem, add an
    asyncio.Queue-based reorder buffer in _reader_loop().
"""
from __future__ import annotations

import asyncio
import logging
import socket
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional

from pipecat.frames.frames import (
    CancelFrame,
    EndFrame,
    Frame,
    InputAudioRawFrame,
    OutputAudioRawFrame,
    StartFrame,
    InterruptionFrame,
)
from pipecat.transports.base_input import BaseInputTransport
from pipecat.transports.base_output import BaseOutputTransport
from pipecat.transports.base_transport import BaseTransport, TransportParams

from voicebot.config import (
    FRAME_SAMPLES,
    RTP_TIMESTAMP_DIFFERENCE,
    SAMPLE_RATE,
    SSRC,
    ULAW_RTP_FRAME_SIZE,
)
from voicebot.transport.rtp_codec import (
    build_rtp_packet,
    decode_ulaw_to_pcm,
    encode_pcm_to_ulaw,
    parse_rtp_header,
    prepare_ulaw_stream_frames,
)

logger = logging.getLogger(__name__)


@dataclass
class AsteriskRTPParams(TransportParams):
    """Transport params — local UDP bind + remote RTP destination.

    Set remote_host + remote_port if you know Asterisk's address at startup.
    Leave them None to use symmetric-RTP auto-learn (most deployments).
    local_port=0 lets the OS assign a free port; the actual port is published
    back to self.local_port after bind so Asterisk can be told where to send.
    """
    local_host: str = "0.0.0.0"
    local_port: int = 0                     # 0 = OS-assigned
    remote_host: Optional[str] = None       # set after Asterisk SDP exchange
    remote_port: Optional[int] = None
    audio_in_enabled: bool = True
    audio_out_enabled: bool = True
    audio_in_sample_rate: int = SAMPLE_RATE
    audio_out_sample_rate: int = SAMPLE_RATE
    audio_in_channels: int = 1
    audio_out_channels: int = 1


class _RTPDatagramProtocol(asyncio.DatagramProtocol):
    """asyncio UDP protocol — pushes raw datagrams onto an asyncio.Queue."""

    def __init__(self, queue: "asyncio.Queue[bytes]", on_remote: Callable[[tuple], None]):
        self._queue = queue
        self._on_remote = on_remote

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data: bytes, addr) -> None:
        # Learn the sender's address for symmetric RTP.
        self._on_remote(addr)
        try:
            self._queue.put_nowait(data)
        except asyncio.QueueFull:
            logger.warning("RTP input queue full — dropping packet")

    def error_received(self, exc):
        logger.warning("RTP UDP error: %s", exc)


class AsteriskRTPInputTransport(BaseInputTransport):
    """
    Binds a UDP port, reads RTP, μ-law-decodes, emits InputAudioRawFrame.

    The queue (maxsize=200) buffers ~4 seconds of 20 ms packets. If the
    pipeline falls behind, older packets are dropped (put_nowait raises QueueFull).
    """

    def __init__(self, params: AsteriskRTPParams):
        super().__init__(params)
        self._params = params
        self._queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=200)
        self._datagram_transport: Optional[asyncio.DatagramTransport] = None
        self._reader_task: Optional[asyncio.Task] = None
        self._learned_remote: Optional[tuple] = None

    @property
    def remote(self) -> Optional[tuple]:
        """The auto-learned {ip, port} of the Asterisk RTP stream."""
        return self._learned_remote

    async def start(self, frame: StartFrame):
        await super().start(frame)
        loop = asyncio.get_running_loop()
        self._datagram_transport, _ = await loop.create_datagram_endpoint(
            lambda: _RTPDatagramProtocol(self._queue, self._on_remote_learned),
            local_addr=(self._params.local_host, self._params.local_port),
        )
        sock: socket.socket = self._datagram_transport.get_extra_info("socket")
        bound_host, bound_port = sock.getsockname()[:2]
        logger.info("AsteriskRTP input bound to %s:%s", bound_host, bound_port)
        # Publish the actual bound port back so the caller can tell Asterisk.
        self._params.local_port = bound_port
        self._reader_task = asyncio.create_task(self._reader_loop())

    async def stop(self, frame: EndFrame):
        await self._shutdown()
        await super().stop(frame)

    async def cancel(self, frame: CancelFrame):
        await self._shutdown()
        await super().cancel(frame)

    async def _shutdown(self):
        if self._reader_task and not self._reader_task.done():
            self._reader_task.cancel()
            try:
                await self._reader_task
            except (asyncio.CancelledError, Exception):
                pass
        if self._datagram_transport is not None:
            self._datagram_transport.close()
            self._datagram_transport = None

    def _on_remote_learned(self, addr: tuple) -> None:
        if self._learned_remote != addr:
            self._learned_remote = addr
            logger.info("AsteriskRTP learned remote = %s", addr)

    async def _reader_loop(self):
        """Dequeue RTP packets, decode μ-law → PCM, push InputAudioRawFrame."""
        while True:
            packet = await self._queue.get()
            try:
                _seq, _ts, _ssrc, payload = parse_rtp_header(packet)
            except ValueError:
                continue
            if not payload:
                continue
            pcm = decode_ulaw_to_pcm(payload)
            await self.push_audio_frame(
                InputAudioRawFrame(
                    audio=pcm,
                    sample_rate=self._params.audio_in_sample_rate,
                    num_channels=self._params.audio_in_channels,
                )
            )


class AsteriskRTPOutputTransport(BaseOutputTransport):
    """
    Receives OutputAudioRawFrame, μ-law-encodes, sends as RTP via UDP.

    Reuses the input socket (shared via get_socket callback) for symmetric RTP.
    Maintains a _pending buffer so μ-law frames are always exactly 160 bytes
    (20 ms) even when TTS emits audio in different chunk sizes.
    """

    def __init__(self, params: AsteriskRTPParams, get_socket: Callable[[], Optional[socket.socket]],
                 get_remote: Callable[[], Optional[tuple]]):
        super().__init__(params)
        self._params = params
        self._get_socket = get_socket
        self._get_remote = get_remote
        self._seq = 0
        self._timestamp = 0
        self._ssrc = SSRC
        self._pending: bytes = b""    # partial frame buffer (< 160 bytes)
        self._send_lock = asyncio.Lock()

    async def start(self, frame: StartFrame):
        await super().start(frame)
        self._seq = 0
        self._timestamp = 0
        self._pending = b""

    async def write_audio_frame(self, frame: OutputAudioRawFrame):
        """Called by Pipecat with PCM at audio_out_sample_rate; encode + send."""
        pcm = frame.audio
        if frame.sample_rate != SAMPLE_RATE:
            # TTS may produce audio at a different sample rate (e.g. 16 kHz).
            from voicebot.transport.rtp_codec import resample_pcm_audio
            pcm = resample_pcm_audio(pcm, frame.sample_rate, SAMPLE_RATE)
        ulaw = encode_pcm_to_ulaw(pcm)
        frames, self._pending = prepare_ulaw_stream_frames(ulaw, self._pending)
        for f in frames:
            await self._send_rtp_frame(f)

    async def process_frame(self, frame: Frame, direction):
        await super().process_frame(frame, direction)
        if isinstance(frame, (InterruptionFrame, CancelFrame)):
            # Drop the partial-frame buffer — mid-utterance audio already
            # queued for sending is discarded on interruption.
            self._pending = b""

    async def _send_rtp_frame(self, ulaw_frame: bytes) -> None:
        sock = self._get_socket()
        remote = self._get_remote()
        if sock is None or remote is None:
            # Remote not yet learned (no inbound packet from Asterisk yet).
            return
        packet = build_rtp_packet(ulaw_frame, self._seq, self._timestamp, self._ssrc)
        async with self._send_lock:
            sock.sendto(packet, remote)
        self._seq = (self._seq + 1) & 0xFFFF
        self._timestamp = (self._timestamp + RTP_TIMESTAMP_DIFFERENCE) & 0xFFFFFFFF


class AsteriskRTPTransport(BaseTransport):
    """
    Glues input and output halves over a single UDP socket.

    Call transport.input() and transport.output() to get the FrameProcessors
    for the pipeline. Both are lazy-constructed the first time they're accessed.

    The output half reuses the input socket so symmetric-RTP NAT traversal
    works: Asterisk sees our packets arriving from the same {ip:port} it sends
    to, which satisfies NAT mappings without needing STUN.
    """

    def __init__(self, params: Optional[AsteriskRTPParams] = None):
        super().__init__()
        self._params = params or AsteriskRTPParams()
        self._input: Optional[AsteriskRTPInputTransport] = None
        self._output: Optional[AsteriskRTPOutputTransport] = None

    def input(self) -> AsteriskRTPInputTransport:
        if self._input is None:
            self._input = AsteriskRTPInputTransport(self._params)
        return self._input

    def output(self) -> AsteriskRTPOutputTransport:
        if self._output is None:
            self._output = AsteriskRTPOutputTransport(
                self._params,
                # Shared socket: output borrows input's UDP socket handle.
                get_socket=lambda: (
                    self._input._datagram_transport.get_extra_info("socket")
                    if self._input and self._input._datagram_transport else None
                ),
                # Remote resolution: prefer explicit params, fall back to
                # auto-learned address from first inbound packet.
                get_remote=lambda: (
                    (self._params.remote_host, self._params.remote_port)
                    if self._params.remote_host and self._params.remote_port
                    else (self._input.remote if self._input else None)
                ),
            )
        return self._output
