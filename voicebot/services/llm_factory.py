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
        from pipecat.services.groq.llm import GroqLLMService  # type: ignore
        return GroqLLMService(api_key=GROQ_API_KEY, model=LLM_MODEL, reasoning_format="parsed", include_reasoning =  False)
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
    if vendor == "sarvam":
        from pipecat.services.sarvam.llm import SarvamLLMService  # type: ignore
        return SarvamLLMService(api_key=SARVAM_LLM_API_KEY, model=LLM_MODEL)
    raise ValueError(f"Unknown LLM_VENDOR={vendor!r}")
