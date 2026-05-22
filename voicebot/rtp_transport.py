"""
RTP/UDP transport for pipecat — connects to Asterisk exactly as the existing
credgenics-voicebot-service does.

Audio format on the wire (matching Asterisk defaults):
  - Payload type 0 = PCMU (G.711 µ-law)
  - 8 kHz, mono, 20 ms per RTP frame (160 bytes of µ-law payload)

Pipecat-internal format:
  - 16 kHz PCM-16 (what Groq STT and Cartesia TTS expect)
  - The transport resamples in both directions using pipecat's SOXRStream resampler

Connection model (stateless UDP, same as existing system):
  - Transport binds a local UDP socket and waits.
  - The first packet received sets the remote address (Asterisk source).
  - All outbound TTS audio is sent back to that address.
  - `on_client_connected` fires when the first valid RTP packet arrives.

Usage::

    transport = RTPUDPTransport(local_addr=("0.0.0.0", 10000))

    @transport.event_handler("on_client_connected")
    async def on_connected(transport, addr):
        await task.queue_frames([LLMContextFrame(context=context)])
"""

import asyncio
import time
from typing import Optional, Tuple

from loguru import logger

from pipecat.audio.utils import create_stream_resampler, pcm_to_ulaw, ulaw_to_pcm
from pipecat.frames.frames import (
    CancelFrame,
    ClientConnectedFrame,
    EndFrame,
    Frame,
    InputAudioRawFrame,
    InterruptionFrame,
    OutputAudioRawFrame,
    StartFrame,
)
from pipecat.transports.base_input import BaseInputTransport
from pipecat.transports.base_output import BaseOutputTransport
from pipecat.transports.base_transport import BaseTransport, TransportParams

# ── RTP / audio constants ────────────────────────────────────────────────────

RTP_HEADER_SIZE = 12
ULAW_FRAME_SIZE = 160          # 20 ms @ 8 kHz, 1 byte/sample (µ-law)
RTP_SAMPLE_RATE = 8000         # Asterisk PCMU sample rate
RTP_TIMESTAMP_INCREMENT = 160  # samples per frame
SSRC = 12_345_678
RTP_SEQ_MAX = 2**16
ULAW_SILENCE = b"\xff"         # µ-law encoding of PCM 0 (silence)


def _build_rtp_packet(payload: bytes, seq: int, timestamp: int, ssrc: int) -> bytes:
    header = bytearray(12)
    header[0] = 0x80  # V=2, P=0, X=0, CC=0
    header[1] = 0x00  # M=0, PT=0 (PCMU)
    header[2] = (seq >> 8) & 0xFF
    header[3] = seq & 0xFF
    header[4] = (timestamp >> 24) & 0xFF
    header[5] = (timestamp >> 16) & 0xFF
    header[6] = (timestamp >> 8) & 0xFF
    header[7] = timestamp & 0xFF
    header[8:12] = ssrc.to_bytes(4, "big")
    return bytes(header) + payload


# ── Internal UDP protocol ────────────────────────────────────────────────────


class _UDPProtocol(asyncio.DatagramProtocol):
    """Thin asyncio DatagramProtocol that forwards packets to a callback."""

    def __init__(self, on_received):
        self._on_received = on_received
        self._transport: Optional[asyncio.DatagramTransport] = None

    def connection_made(self, transport: asyncio.DatagramTransport):
        self._transport = transport
        sockname = transport.get_extra_info("sockname")
        logger.info(f"RTPUDPTransport listening on {sockname[0]}:{sockname[1]}")

    def datagram_received(self, data: bytes, addr: Tuple[str, int]):
        self._on_received(data, addr)

    def error_received(self, exc: Exception):
        logger.error(f"RTP UDP error: {exc}")

    def connection_lost(self, _exc):
        logger.debug("RTP UDP socket closed")

    def sendto(self, data: bytes, addr: Tuple[str, int]):
        if self._transport and not self._transport.is_closing():
            self._transport.sendto(data, addr)

    def close(self):
        if self._transport and not self._transport.is_closing():
            self._transport.close()


# ── Params ───────────────────────────────────────────────────────────────────


class RTPUDPParams(TransportParams):
    """Configuration for RTPUDPTransport.

    audio_in_sample_rate  — rate delivered to the pipeline (Groq STT: 16 kHz)
    audio_out_sample_rate — rate expected from TTS (Cartesia default: 16 kHz)
    """

    audio_in_enabled: bool = True
    audio_out_enabled: bool = True
    audio_in_sample_rate: int = 16000
    audio_out_sample_rate: int = 16000


# ── Input transport ──────────────────────────────────────────────────────────


class RTPUDPInputTransport(BaseInputTransport):
    def __init__(self, rtp_transport: "RTPUDPTransport", params: RTPUDPParams, **kwargs):
        super().__init__(params, **kwargs)
        self._rtp = rtp_transport
        self._in_resampler = create_stream_resampler()
        self._receive_task = None
        self._initialized = False

    async def start(self, frame: StartFrame):
        await super().start(frame)
        if self._initialized:
            return
        self._initialized = True
        await self._rtp._bind_socket()
        self._receive_task = self.create_task(self._receive_loop())
        await self.set_transport_ready(frame)

    async def stop(self, frame: EndFrame):
        await super().stop(frame)
        await self._stop_receive()

    async def cancel(self, frame: CancelFrame):
        await super().cancel(frame)
        await self._stop_receive()

    async def cleanup(self):
        await super().cleanup()
        await self._rtp.cleanup()

    async def _stop_receive(self):
        if self._receive_task:
            await self.cancel_task(self._receive_task)
            self._receive_task = None

    async def _receive_loop(self):
        queue = self._rtp._queue
        first = True
        try:
            while True:
                data, addr = await queue.get()

                if first:
                    first = False
                    self._rtp._set_remote_addr(addr)
                    await self.push_frame(ClientConnectedFrame())
                    await self._rtp._call_event_handler("on_client_connected", addr)

                if len(data) <= RTP_HEADER_SIZE:
                    continue

                payload = data[RTP_HEADER_SIZE:]
                pcm = await ulaw_to_pcm(
                    payload,
                    RTP_SAMPLE_RATE,
                    self._params.audio_in_sample_rate,
                    self._in_resampler,
                )
                await self.push_audio_frame(
                    InputAudioRawFrame(
                        audio=pcm,
                        sample_rate=self._params.audio_in_sample_rate,
                        num_channels=1,
                    )
                )
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("RTP receive loop error")


# ── Output transport ─────────────────────────────────────────────────────────


class RTPUDPOutputTransport(BaseOutputTransport):
    def __init__(self, rtp_transport: "RTPUDPTransport", params: RTPUDPParams, **kwargs):
        super().__init__(params, **kwargs)
        self._rtp = rtp_transport
        self._out_resampler = create_stream_resampler()
        self._seq = 0
        self._timestamp = 0
        self._pending: bytes = b""
        self._initialized = False
        self._next_send_time: float = 0.0

    async def start(self, frame: StartFrame):
        await super().start(frame)
        if self._initialized:
            return
        self._initialized = True
        self._pending = b""
        await self.set_transport_ready(frame)

    async def stop(self, frame: EndFrame):
        await super().stop(frame)
        await self._rtp.cleanup()

    async def cancel(self, frame: CancelFrame):
        await super().cancel(frame)
        await self._rtp.cleanup()

    async def cleanup(self):
        await super().cleanup()
        await self._rtp.cleanup()

    async def process_frame(self, frame: Frame, direction):
        await super().process_frame(frame, direction)
        if isinstance(frame, (InterruptionFrame, CancelFrame)):
            self._pending = b""

    async def write_audio_frame(self, frame: OutputAudioRawFrame) -> bool:
        remote_addr = self._rtp._remote_addr
        protocol = self._rtp._protocol
        if remote_addr is None or protocol is None:
            return False

        # Resample TTS PCM (16 kHz) → 8 kHz, then encode to µ-law
        ulaw = await pcm_to_ulaw(
            frame.audio, frame.sample_rate, RTP_SAMPLE_RATE, self._out_resampler
        )

        # Accumulate with leftover from previous frame, emit only full 160-byte chunks
        combined = self._pending + ulaw
        full = (len(combined) // ULAW_FRAME_SIZE) * ULAW_FRAME_SIZE
        self._pending = combined[full:]
        for i in range(0, full, ULAW_FRAME_SIZE):
            chunk = combined[i : i + ULAW_FRAME_SIZE]
            packet = _build_rtp_packet(chunk, self._seq, self._timestamp, SSRC)
            now = time.monotonic()
            if self._next_send_time > now:
                await asyncio.sleep(self._next_send_time - now)
            protocol.sendto(packet, remote_addr)
            self._next_send_time = max(time.monotonic(), self._next_send_time) + 0.020
            self._seq = (self._seq + 1) % RTP_SEQ_MAX
            self._timestamp = (self._timestamp + RTP_TIMESTAMP_INCREMENT) & 0xFFFFFFFF

        return True


# ── Public transport class ───────────────────────────────────────────────────


class RTPUDPTransport(BaseTransport):
    """Pipecat transport for bidirectional RTP/PCMU audio with Asterisk over UDP.

    Mirrors the behaviour of credgenics-voicebot-service's UDPServerProtocol +
    RTPProcessor pair: listen on a fixed UDP port and stream TTS audio back to
    the remote side.

    Two modes for the send address:
    - ``remote_addr`` provided: use that fixed address immediately (matches the
      local test setup where send_mic_audio.py and play_audio.py use separate
      ports, and the bot must send to the playback port explicitly).
    - ``remote_addr=None``: learn the address from the first incoming packet
      (standard Asterisk behaviour — Asterisk declares its RTP receive port in
      SDP and we send back there).

    Events:
        on_client_connected(transport, addr): fired on the first RTP packet.
    """

    def __init__(
        self,
        local_addr: Tuple[str, int],
        remote_addr: Optional[Tuple[str, int]] = None,
        params: Optional[RTPUDPParams] = None,
        input_name: Optional[str] = None,
        output_name: Optional[str] = None,
    ):
        super().__init__(input_name=input_name, output_name=output_name)
        self._local_addr = local_addr
        self._params = params or RTPUDPParams()
        self._queue: asyncio.Queue[Tuple[bytes, Tuple[str, int]]] = asyncio.Queue()
        self._protocol: Optional[_UDPProtocol] = None
        # Pre-configured send address (fixed) or learned from first packet (None).
        self._remote_addr: Optional[Tuple[str, int]] = remote_addr
        self._cleanup_done = False

        self._input_proc = RTPUDPInputTransport(
            self, self._params, name=self._input_name
        )
        self._output_proc = RTPUDPOutputTransport(
            self, self._params, name=self._output_name
        )

        self._register_event_handler("on_client_connected")

    def input(self) -> RTPUDPInputTransport:
        return self._input_proc

    def output(self) -> RTPUDPOutputTransport:
        return self._output_proc

    # ── Internal helpers ────────────────────────────────────────────────────

    def _on_datagram(self, data: bytes, addr: Tuple[str, int]):
        self._queue.put_nowait((data, addr))

    def _set_remote_addr(self, addr: Tuple[str, int]):
        if self._remote_addr is None:
            self._remote_addr = addr
            logger.info(f"RTP remote address: {addr[0]}:{addr[1]}")

    async def _bind_socket(self):
        if self._protocol is not None:
            return
        loop = asyncio.get_running_loop()
        self._protocol = _UDPProtocol(self._on_datagram)
        await loop.create_datagram_endpoint(
            lambda: self._protocol,
            local_addr=self._local_addr,
        )

    async def cleanup(self):
        if self._cleanup_done:
            return
        self._cleanup_done = True
        if self._protocol:
            self._protocol.close()
