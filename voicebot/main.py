"""
voicebot.main — production entrypoint (Asterisk RTP).

For local browser-based smoke testing, use voicebot/run_webrtc.py instead.

  python -m voicebot.main --local-port 4000 --remote 10.0.0.5:40000 --call-id abc
"""
from __future__ import annotations

import argparse
import asyncio
import sys

from voicebot.observability import setup_logging
from voicebot.pipeline import build_and_run, load_lid_model
from voicebot.rtp_transport import RTPUDPTransport


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="voicebot — Asterisk RTP runner")
    p.add_argument("--local-port", type=int, default=0,
                   help="local UDP port for RTP (0 = OS-assigned)")
    p.add_argument("--remote", type=str, default=None,
                   help="remote host:port (auto-learn from inbound RTP if omitted)")
    p.add_argument("--call-id", type=str, default="asterisk-dev",
                   help="conversation key in Redis")
    p.add_argument("--no-lid", action="store_true", help="skip SpeechBrain LID load")
    return p.parse_args(argv)


def main(argv: list[str]) -> None:
    setup_logging()
    args = parse_args(argv)

    remote_addr = None
    if args.remote:
        host, port = args.remote.rsplit(":", 1)
        remote_addr = (host, int(port))

    transport = RTPUDPTransport(
        local_addr=("0.0.0.0", args.local_port),
        remote_addr=remote_addr,
    )
    lid_model = load_lid_model(args.no_lid)

    asyncio.run(build_and_run(transport, args.call_id, lid_model, handle_sigint=True))


if __name__ == "__main__":
    main(sys.argv[1:])
