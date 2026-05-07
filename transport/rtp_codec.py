"""
μ-law ↔ PCM and RTP framing.

Ported byte-for-byte from credgenics-voice-bot-library/src/cg_voicebot/rtp_utils.py
(see lines 43, 57, 63, 105, 236, 255, 338, 316). All other helpers in that file
(VAD wrappers, pydub resampling, S3 uploads, language detection, sentence
breakers) are not needed in the Pipecat orchestration.
"""
from __future__ import annotations

import audioop
from typing import List, Optional, Tuple

import numpy as np

from voicebot.config import (
    CHANNELS,
    FRAME_SAMPLES,
    SAMPLE_RATE,
    SAMPLE_WIDTH,
    ULAW_RTP_FRAME_SIZE,
)
PCM_SILENCE = b"\x00\x00" * FRAME_SAMPLES

INT16_TO_FLOAT32 = 1.0 / 32768.0


def build_rtp_packet(payload: bytes, seq: int, timestamp: int, ssrc: int) -> bytes:
    header = bytearray(12)
    header[0] = 0x80  # Version 2
    header[1] = 0x00  # Payload type 0 (PCMU / μ-law)
    header[2] = (seq >> 8) & 0xFF
    header[3] = seq & 0xFF
    header[4] = (timestamp >> 24) & 0xFF
    header[5] = (timestamp >> 16) & 0xFF
    header[6] = (timestamp >> 8) & 0xFF
    header[7] = timestamp & 0xFF
    header[8:12] = ssrc.to_bytes(4, "big")
    return bytes(header) + payload


def parse_rtp_header(packet: bytes) -> Tuple[int, int, int, bytes]:
    if len(packet) < 12:
        raise ValueError(f"RTP packet too short: {len(packet)} bytes")
    seq = (packet[2] << 8) | packet[3]
    timestamp = (packet[4] << 24) | (packet[5] << 16) | (packet[6] << 8) | packet[7]
    ssrc = int.from_bytes(packet[8:12], "big")
    return seq, timestamp, ssrc, packet[12:]


def encode_pcm_to_ulaw(pcm: bytes) -> bytes:
    """16-bit linear PCM → μ-law (8 kHz, mono)."""
    return audioop.lin2ulaw(pcm, SAMPLE_WIDTH)


def decode_ulaw_to_pcm(ulaw: bytes) -> bytes:
    """μ-law → 16-bit linear PCM."""
    return audioop.ulaw2lin(ulaw, SAMPLE_WIDTH)


def split_ulaw_into_frames(ulaw: bytes) -> List[bytes]:
    """Split μ-law byte string into 20 ms (160 byte) RTP-sized frames."""
    return [ulaw[i:i + ULAW_RTP_FRAME_SIZE] for i in range(0, len(ulaw), ULAW_RTP_FRAME_SIZE)]


def prepare_ulaw_stream_frames(
    audio_response: bytes,
    pending_audio: bytes = b"",
) -> Tuple[List[bytes], bytes]:
    """
    Frame streaming 8 kHz μ-law audio without emitting short RTP frames.

    Returns (full-frames, leftover) — leftover should be passed back as
    `pending_audio` on the next call.
    """
    if not audio_response and not pending_audio:
        return [], b""
    audio = pending_audio + audio_response
    full_frame_bytes = (len(audio) // ULAW_RTP_FRAME_SIZE) * ULAW_RTP_FRAME_SIZE
    frames = [
        audio[i:i + ULAW_RTP_FRAME_SIZE]
        for i in range(0, full_frame_bytes, ULAW_RTP_FRAME_SIZE)
    ]
    return frames, audio[full_frame_bytes:]


def flush_ulaw_stream_frames(pending_audio: bytes) -> List[bytes]:
    """Pad the final partial μ-law frame with silence at stream end."""
    if not pending_audio:
        return []
    silence = ulaw_silence_frame()
    missing = ULAW_RTP_FRAME_SIZE - len(pending_audio)
    return [pending_audio + silence[:missing]]


def ulaw_silence_frame() -> bytes:
    """20 ms μ-law silence frame (160 bytes)."""
    return audioop.lin2ulaw(PCM_SILENCE, SAMPLE_WIDTH)


def resample_pcm_audio(
    audio: bytes,
    source_sample_rate: int,
    target_sample_rate: int = SAMPLE_RATE,
    sample_width: int = SAMPLE_WIDTH,
    channels: int = CHANNELS,
) -> bytes:
    """Resample raw PCM via audioop.ratecv (no pydub)."""
    if not audio or source_sample_rate == target_sample_rate:
        return audio
    resampled, _ = audioop.ratecv(
        audio, sample_width, channels,
        source_sample_rate, target_sample_rate, None,
    )
    return resampled


def rtp_payload_to_pcm_float32(payload: bytes) -> Optional[np.ndarray]:
    """Decode an RTP μ-law payload to a float32 [-1, 1] numpy array."""
    try:
        pcm = decode_ulaw_to_pcm(payload)
        i16 = np.frombuffer(pcm, dtype=np.int16)
        return i16.astype(np.float32) * INT16_TO_FLOAT32
    except Exception:
        return None
