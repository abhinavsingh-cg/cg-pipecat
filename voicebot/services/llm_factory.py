"""
LLM factory — returns a stock Pipecat LLMService based on LLM_VENDOR.

All four vendors ship as Pipecat services, so this is a thin selector.
Replaces the custom LLMVendorFactory in cg_voicebot/llm_vendor.py:349.

TO ADD A NEW VENDOR:
  1. pip install "pipecat-ai[your-vendor]" (or add the extra to requirements.txt)
  2. Add a new `if vendor == "yourvendor":` block below.
  3. Set LLM_VENDOR=yourvendor in .env.
  4. Add any new API key vars to config.py.
"""
from __future__ import annotations

import logging
from pipecat.services.llm_service import LLMService

from voicebot.config import (
    AWS_BEDROCK_API_KEY,
    AWS_BEDROCK_REGION,
    GROQ_API_KEY,
    LLM_MODEL,
    LLM_VENDOR,
    OPENAI_API_KEY,
    OPENROUTER_API_KEY,
    SARVAM_LLM_API_KEY,
)

logger = logging.getLogger(__name__)


def build_llm() -> LLMService:
    """
    Returns the configured LLM service.

    LLM_MODEL is passed straight to the vendor; valid values depend on the
    vendor (e.g. "llama-3.1-70b-versatile" for Groq, "gpt-4o" for OpenAI).
    """
    vendor = LLM_VENDOR
    if vendor == "groq":
        import time
        from pipecat.services.groq.llm import GroqLLMService  # type: ignore

        _HEADER_KEYS = (
            "x-request-id",
            "x-groq-region",
            "x-ratelimit-limit-requests",
            "x-ratelimit-remaining-requests",
            "x-ratelimit-limit-tokens",
            "x-ratelimit-remaining-tokens",
            "x-ratelimit-reset-tokens",
            "retry-after",
        )

        class _TimedStream:
            def __init__(self, inner, t_sent: float, t_open: float):
                self._inner = inner
                self._t_sent = t_sent
                self._t_open = t_open

                hdrs = {}
                resp = getattr(inner, "response", None)
                if resp is not None and getattr(resp, "headers", None) is not None:
                    for k in _HEADER_KEYS:
                        v = resp.headers.get(k)
                        if v is not None:
                            hdrs[k] = v
                logger.info(
                    "groq_request | request_open=%.0fms | headers=%s",
                    (t_open - t_sent) * 1000,
                    hdrs,
                )

            def __aiter__(self):
                src = self._inner.__aiter__()
                t_sent = self._t_sent
                state = {"first_ms": None, "last_chunk": None}

                async def _gen():
                    async for chunk in src:
                        if state["first_ms"] is None:
                            state["first_ms"] = (time.perf_counter() - t_sent) * 1000
                        state["last_chunk"] = chunk
                        yield chunk
                    last = state["last_chunk"]
                    usage = getattr(last, "usage", None) if last is not None else None
                    server = {}
                    if usage is not None:
                        for k in ("queue_time", "prompt_time", "completion_time", "total_time"):
                            v = getattr(usage, k, None)
                            if v is not None:
                                server[k + "_ms"] = round(v * 1000)
                    wall_total_ms = (time.perf_counter() - t_sent) * 1000
                    network_ms = None
                    if "total_time_ms" in server:
                        network_ms = round(wall_total_ms - server["total_time_ms"])
                    logger.info(
                        "groq_timing | wall_ttfb=%.0fms | wall_total=%.0fms | server=%s | network~=%sms",
                        state["first_ms"] if state["first_ms"] is not None else -1,
                        wall_total_ms,
                        server,
                        network_ms,
                    )
                return _gen()

            async def close(self):
                if hasattr(self._inner, "close"):
                    await self._inner.close()
                elif hasattr(self._inner, "aclose"):
                    await self._inner.aclose()

            async def aclose(self):
                await self.close()

        class TimingGroqLLMService(GroqLLMService):
            async def get_chat_completions(self, context):
                logger.info(f"""{context=}""")
                t_sent = time.perf_counter()
                stream = await super().get_chat_completions(context)
                t_open = time.perf_counter()
                return _TimedStream(stream, t_sent, t_open)

        # Groq-specific request params (reasoning_format, include_reasoning) must
        # be passed via settings.extra — kwargs to the ctor are silently dropped
        # because Pipecat only forwards `settings.extra` into chat.completions.create().
        # Without this, Qwen-32B (a reasoning model) streams <think>...</think>
        # tokens straight into TTS.
        settings = GroqLLMService.Settings(
            model=LLM_MODEL,
            # extra={
            #     # Groq-specific params aren't in the OpenAI SDK's typed signature,
            #     # so they have to ride along in extra_body to reach the request JSON.
            #     "extra_body": {
            #         # "reasoning_format": "parsed",
            #         "include_reasoning": False,
            #     },
            # },
        )
        return TimingGroqLLMService(api_key=GROQ_API_KEY, settings=settings)
    if vendor == "openai":
        from pipecat.services.openai.llm import OpenAILLMService  # type: ignore
        return OpenAILLMService(api_key=OPENAI_API_KEY, model=LLM_MODEL)
    if vendor == "bedrock":
        from pipecat.services.aws.llm import AWSBedrockLLMService  # type: ignore
        return AWSBedrockLLMService(
            api_key=AWS_BEDROCK_API_KEY,
            region=AWS_BEDROCK_REGION,
            model=LLM_MODEL,
        )
    if vendor == "openrouter":
        from pipecat.services.openrouter.llm import OpenRouterLLMService  # type: ignore
        return OpenRouterLLMService(api_key=OPENROUTER_API_KEY, model=LLM_MODEL)
    if vendor == "sarvam":
        from pipecat.services.sarvam.llm import SarvamLLMService  # type: ignore
        return SarvamLLMService(api_key=SARVAM_LLM_API_KEY, model=LLM_MODEL)
    raise ValueError(f"Unknown LLM_VENDOR={vendor!r}")
