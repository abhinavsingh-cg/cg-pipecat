"""
JSON logging for voicebot.

Single setup_logging() — Pipecat emits MetricsFrame events through the normal
pipeline; downstream observers can subscribe via PipelineTask hooks if richer
telemetry is needed. OTEL is wired only when OTEL_EXPORTER_OTLP_ENDPOINT is set.
"""
from __future__ import annotations

import logging
import os

from pythonjsonlogger import jsonlogger

from voicebot.config import (
    LOG_FILE,
    LOG_LEVEL,
    OTEL_EXPORTER_OTLP_ENDPOINT,
    OTEL_SERVICE_NAME,
)


def setup_logging() -> logging.Logger:
    """
    Configure stdlib logging + redirect Pipecat's loguru into the same sink
    so every log line shows up in one place.
    """
    logger = logging.getLogger("voicebot")
    logger.setLevel(getattr(logging, LOG_LEVEL.upper(), logging.INFO))

    formatter = jsonlogger.JsonFormatter(
        fmt="%(asctime)s %(name)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
        json_ensure_ascii=False,
    )
    root = logging.getLogger()
    root.setLevel(getattr(logging, LOG_LEVEL.upper(), logging.INFO))
    if not root.handlers:
        stdout = logging.StreamHandler()
        stdout.setFormatter(formatter)
        root.addHandler(stdout)

    if LOG_FILE:
        os.makedirs(os.path.dirname(LOG_FILE) or ".", exist_ok=True)
        fh = logging.FileHandler(LOG_FILE)
        fh.setFormatter(formatter)
        root.addHandler(fh)

    # Pipecat uses loguru — bridge it into stdlib so its messages reach the
    # same handlers (and our log file).
    try:
        from loguru import logger as _loguru
        _loguru.remove()

        class _InterceptHandler(logging.Handler):
            def emit(self, record):
                pass

        def _sink(message):
            r = message.record
            stdlib = logging.getLogger("pipecat")
            stdlib.log(
                logging.getLevelName(r["level"].name),
                "{}:{}:{} - {}".format(r["name"], r["function"], r["line"], r["message"]),
            )
        _loguru.add(_sink, level=LOG_LEVEL.upper())
    except Exception as exc:
        logger.warning("loguru bridge failed: %s", exc)

    if OTEL_EXPORTER_OTLP_ENDPOINT:
        try:
            _setup_otel()
        except Exception as exc:
            logger.warning("OTEL setup failed: %s", exc)

    return logger


def _setup_otel() -> None:
    from opentelemetry import trace
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

    resource = Resource.create({"service.name": OTEL_SERVICE_NAME})
    provider = TracerProvider(resource=resource)
    provider.add_span_processor(BatchSpanProcessor(
        OTLPSpanExporter(endpoint=OTEL_EXPORTER_OTLP_ENDPOINT)
    ))
    trace.set_tracer_provider(provider)
