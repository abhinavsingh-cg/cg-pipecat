"""
AsteriskRTPTransport — a Pipecat transport that speaks raw RTP over UDP.

Asterisk already terminates SIP and produces μ-law @ 8 kHz / 20 ms RTP. We
plug the agent in directly via UDP — no LiveKit SIP / Daily / Twilio proxy.

Replaces the entire `RTPProcessor` from credgenics-voice-bot-library
(rtp_processor.py:893+ — ~2800 lines) with a thin transport. VAD, turn
detection, audio framing, jitter, and barge-in cancellation are handled
inside Pipecat once we hand it `InputAudioRawFrame`s.
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
    """Transport params — local UDP bind + remote RTP destination."""

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
    """Async UDP datagram protocol — pushes inbound RTP onto a queue."""

    def __init__(self, queue: "asyncio.Queue[bytes]", on_remote: Callable[[tuple], None]):
        self._queue = queue
        self._on_remote = on_remote

    def connection_made(self, transport):  # noqa: D401
        self.transport = transport

    def datagram_received(self, data: bytes, addr) -> None:
        # Auto-learn the remote endpoint on first packet (symmetric RTP).
        self._on_remote(addr)
        try:
            self._queue.put_nowait(data)
        except asyncio.QueueFull:
            logger.warning("RTP input queue full — dropping packet")

    def error_received(self, exc):  # noqa: D401
        logger.warning("RTP UDP error: %s", exc)


class AsteriskRTPInputTransport(BaseInputTransport):
    """Reads RTP from UDP, μ-law-decodes, emits InputAudioRawFrame."""

    def __init__(self, params: AsteriskRTPParams):
        super().__init__(params)
        self._params = params
        self._queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=200)
        self._datagram_transport: Optional[asyncio.DatagramTransport] = None
        self._reader_task: Optional[asyncio.Task] = None
        self._learned_remote: Optional[tuple] = None

    @property
    def remote(self) -> Optional[tuple]:
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
        self._params.local_port = bound_port  # publish actual port back
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
        """Pull RTP datagrams off the queue, push as InputAudioRawFrame."""
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
    """Receives OutputAudioRawFrame, μ-law-encodes, sends as RTP via UDP."""

    def __init__(self, params: AsteriskRTPParams, get_socket: Callable[[], Optional[socket.socket]],
                 get_remote: Callable[[], Optional[tuple]]):
        super().__init__(params)
        self._params = params
        self._get_socket = get_socket
        self._get_remote = get_remote
        self._seq = 0
        self._timestamp = 0
        self._ssrc = SSRC
        self._pending: bytes = b""
        self._send_lock = asyncio.Lock()

    async def start(self, frame: StartFrame):
        await super().start(frame)
        self._seq = 0
        self._timestamp = 0
        self._pending = b""

    async def write_audio_frame(self, frame: OutputAudioRawFrame):
        """Pipecat hands us PCM at audio_out_sample_rate; convert + send."""
        pcm = frame.audio
        if frame.sample_rate != SAMPLE_RATE:
            from voicebot.transport.rtp_codec import resample_pcm_audio
            pcm = resample_pcm_audio(pcm, frame.sample_rate, SAMPLE_RATE)
        ulaw = encode_pcm_to_ulaw(pcm)
        frames, self._pending = prepare_ulaw_stream_frames(ulaw, self._pending)
        for f in frames:
            await self._send_rtp_frame(f)

    async def process_frame(self, frame: Frame, direction):  # noqa: D401
        await super().process_frame(frame, direction)
        if isinstance(frame, (InterruptionFrame, CancelFrame)):
            # Drop the partial-frame buffer — interrupted mid-utterance.
            self._pending = b""

    async def _send_rtp_frame(self, ulaw_frame: bytes) -> None:
        sock = self._get_socket()
        remote = self._get_remote()
        if sock is None or remote is None:
            # Asterisk hasn't sent us a packet yet — we don't know where to reply.
            return
        packet = build_rtp_packet(ulaw_frame, self._seq, self._timestamp, self._ssrc)
        async with self._send_lock:
            sock.sendto(packet, remote)
        self._seq = (self._seq + 1) & 0xFFFF
        self._timestamp = (self._timestamp + RTP_TIMESTAMP_DIFFERENCE) & 0xFFFFFFFF


class AsteriskRTPTransport(BaseTransport):
    """
    Glues the input and output halves over a single UDP socket.

    Output reuses the input socket so symmetric-RTP NAT traversal works
    (Asterisk's most common configuration).
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
                get_socket=lambda: (
                    self._input._datagram_transport.get_extra_info("socket")
                    if self._input and self._input._datagram_transport else None
                ),
                get_remote=lambda: (
                    (self._params.remote_host, self._params.remote_port)
                    if self._params.remote_host and self._params.remote_port
                    else (self._input.remote if self._input else None)
                ),
            )
        return self._output
