"""
voicebot.profiling — function timing, line profiling, session lifecycle.

Usage (entry points):
    from voicebot.profiling import (
        init_session,
        install_shutdown_handler,
        timed,
        TimedFrameProcessor,
    )

    init_session(call_id="abc", profile=False)   # creates logs/<session_id>.log
    install_shutdown_handler()                     # Ctrl+C once = save + exit
"""
from __future__ import annotations

import functools
import logging
import os
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

logger = logging.getLogger("voicebot.profiling")

# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------
_session_id: str = ""
_session_start: float = 0.0
_log_dir: Path = Path("logs")
_profile_enabled: bool = False
_shutdown_installed: bool = False
_first_sigint: bool = True

# line_profiler (lazy — only imported when --profile is used)
_lp: Any = None


def get_session_id() -> str:
    return _session_id


def is_profile_enabled() -> bool:
    return _profile_enabled


# ---------------------------------------------------------------------------
# Session init
# ---------------------------------------------------------------------------
def init_session(
    call_id: str = "dev",
    profile: bool = False,
    log_dir: str = "logs",
) -> str:
    """
    Generate session ID, create log directory, optionally enable line_profiler.

    Returns the session_id string.
    """
    global _session_id, _session_start, _log_dir, _profile_enabled, _lp

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    _session_id = f"{ts}_{call_id}"
    _session_start = time.monotonic()
    _log_dir = Path(log_dir)
    _log_dir.mkdir(parents=True, exist_ok=True)
    _profile_enabled = profile

    if profile:
        try:
            from line_profiler import LineProfiler
            _lp = LineProfiler()
            logger.info("line_profiler enabled for session %s", _session_id)
        except ImportError:
            logger.warning("line_profiler not installed — `pip install line-profiler`")
            _profile_enabled = False

    # Setup per-session log file
    _setup_session_file_handler()

    logger.info(
        "session_start | session_id=%s | profile=%s | log_dir=%s",
        _session_id, _profile_enabled, _log_dir,
    )
    return _session_id


def _setup_session_file_handler() -> None:
    """Add a FileHandler for this session's log file."""
    if not _session_id:
        return
    try:
        from voicebot.observability import setup_session_file_handler
        setup_session_file_handler(_session_id, str(_log_dir))
    except ImportError:
        # Fallback if observability not available
        root = logging.getLogger()
        formatter = root.handlers[0].formatter if root.handlers else None
        fh = logging.FileHandler(_log_dir / f"{_session_id}.log", encoding="utf-8")
        if formatter:
            fh.setFormatter(formatter)
        root.addHandler(fh)


# ---------------------------------------------------------------------------
# @timed decorator — always-on function timing
# ---------------------------------------------------------------------------
def timed(func: Callable) -> Callable:
    """
    Decorator that logs start/end/duration_ms of every call.
    Works on sync and async functions.
    """
    fname = func.__qualname__

    if asyncio_is_coroutine(func):
        @functools.wraps(func)
        async def async_wrapper(*args, **kwargs):
            t0 = time.perf_counter()
            logger.info("[TIMING] %s | status=start", fname)
            try:
                result = await func(*args, **kwargs)
                elapsed_ms = (time.perf_counter() - t0) * 1000
                logger.info("[TIMING] %s | status=end | duration_ms=%.2f", fname, elapsed_ms)
                return result
            except Exception as exc:
                elapsed_ms = (time.perf_counter() - t0) * 1000
                logger.error("[TIMING] %s | status=error | duration_ms=%.2f | err=%s", fname, elapsed_ms, exc)
                raise
        # Register with line_profiler if active
        _maybe_add_profile(async_wrapper)
        return async_wrapper
    else:
        @functools.wraps(func)
        def sync_wrapper(*args, **kwargs):
            t0 = time.perf_counter()
            logger.info("[TIMING] %s | status=start", fname)
            try:
                result = func(*args, **kwargs)
                elapsed_ms = (time.perf_counter() - t0) * 1000
                logger.info("[TIMING] %s | status=end | duration_ms=%.2f", fname, elapsed_ms)
                return result
            except Exception as exc:
                elapsed_ms = (time.perf_counter() - t0) * 1000
                logger.error("[TIMING] %s | status=error | duration_ms=%.2f | err=%s", fname, elapsed_ms, exc)
                raise
        _maybe_add_profile(sync_wrapper)
        return sync_wrapper


def asyncio_is_coroutine(func: Callable) -> bool:
    import asyncio
    return asyncio.iscoroutinefunction(func)


# ---------------------------------------------------------------------------
# profile decorator — add function to line_profiler
# ---------------------------------------------------------------------------
def profile(func: Callable) -> Callable:
    """
    Mark a function for line-by-line profiling.
    Only active when --profile flag is passed.
    No-op otherwise.
    """
    if _lp is not None:
        _lp.add_function(func)
    return func


def _maybe_add_profile(func: Callable) -> None:
    """Register a function with line_profiler if profiling is active."""
    if _lp is not None:
        try:
            _lp.add_function(func)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# TimedFrameProcessor — wrap any FrameProcessor with timing
# ---------------------------------------------------------------------------
class TimedFrameProcessor:
    """
    Wraps a Pipecat FrameProcessor and times every process_frame call.

    Usage in pipeline.py:
        procs.append(tap(stt, "STTService"))
        procs.append(tap(llm, "LLMService"))
    """
    def __init__(self, processor: Any, label: str):
        self._processor = processor
        self._label = label
        self._call_count = 0
        self._total_ms = 0.0
        self._max_ms = 0.0

    def __getattr__(self, name: str) -> Any:
        """Proxy all other attribute access to the wrapped processor."""
        return getattr(self._processor, name)

    async def process_frame(self, frame: Any, direction: Any) -> None:
        t0 = time.perf_counter()
        await self._processor.process_frame(frame, direction)
        elapsed_ms = (time.perf_counter() - t0) * 1000
        self._call_count += 1
        self._total_ms += elapsed_ms
        if elapsed_ms > self._max_ms:
            self._max_ms = elapsed_ms
        frame_name = type(frame).__name__
        logger.info(
            "[TIMING] %s | frame=%s | duration_ms=%.2f | total_calls=%d | max_ms=%.2f | avg_ms=%.2f",
            self._label, frame_name, elapsed_ms, self._call_count, self._max_ms,
            self._total_ms / self._call_count,
        )

    async def push_frame(self, frame: Any, direction: Any = None) -> None:
        await self._processor.push_frame(frame, direction)


def tap(processor: Any, label: str) -> Any:
    """
    Wrap a processor with timing if it has a process_frame method.
    Returns the original processor if wrapping fails.
    """
    if hasattr(processor, "process_frame"):
        return TimedFrameProcessor(processor, label)
    logger.warning("[TIMING] cannot wrap %s — no process_frame method", label)
    return processor


# ---------------------------------------------------------------------------
# Session summary — printed/logged on shutdown
# ---------------------------------------------------------------------------
def _session_summary() -> None:
    if not _session_id:
        return
    elapsed_s = time.monotonic() - _session_start
    logger.info("=" * 60)
    logger.info("SESSION_SUMMARY | session_id=%s | total_runtime_s=%.2f", _session_id, elapsed_s)
    logger.info("=" * 60)


# ---------------------------------------------------------------------------
# Shutdown handler — dump profile, flush logs, exit
# ---------------------------------------------------------------------------
def install_shutdown_handler() -> None:
    """
    Install SIGINT handler:
      1st Ctrl+C: dump profile stats, log summary, flush handlers, exit(0)
      2nd Ctrl+C: force exit(1)
    """
    global _shutdown_installed
    if _shutdown_installed:
        return
    _shutdown_installed = True

    def _handler(signum: int, frame: Any) -> None:
        global _first_sigint
        if not _first_sigint:
            logger.warning("second SIGINT — forcing exit")
            sys.exit(1)
        _first_sigint = False
        logger.info("SIGINT received — saving session and shutting down...")

        # Dump line_profiler stats
        _dump_profile_stats()

        # Log session summary
        _session_summary()

        # Flush all logging handlers
        for h in logging.getLogger().handlers:
            h.flush()

        sys.exit(0)

    signal.signal(signal.SIGINT, _handler)
    logger.info("shutdown handler installed (Ctrl+C once = save + exit)")


def _dump_profile_stats() -> None:
    """Dump line_profiler stats to logs/ if profiling is active."""
    if _lp is None or not _session_id:
        return
    stats_path = _log_dir / f"profile_{_session_id}.lprof"
    try:
        _lp.dump_stats(str(stats_path))
        logger.info("line_profiler stats saved to %s", stats_path)
        # Also print a text summary to the log
        from io import StringIO
        import contextlib
        buf = StringIO()
        _lp.print_stats(stream=buf, stripzeros=True)
        text_stats_path = _log_dir / f"profile_{_session_id}.txt"
        text_stats_path.write_text(buf.getvalue())
        logger.info("line_profiler text report saved to %s", text_stats_path)
    except Exception as exc:
        logger.warning("failed to dump line_profiler stats: %s", exc)


def session_cleanup() -> None:
    """Call this on normal (non-SIGINT) session end too."""
    _dump_profile_stats()
    _session_summary()
    for h in logging.getLogger().handlers:
        h.flush()
