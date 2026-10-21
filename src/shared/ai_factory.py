"""AI-provider factory — the single switch between Bedrock and the fallbacks.

Every previous ``BedrockClient(settings)`` call site keeps working unchanged by
importing this factory instead (already true because the interface is mirrored).
``AI_PROVIDER`` setting: ``bedrock`` (default) | ``gemini`` | ``groq``.
"""
from __future__ import annotations

from typing import Any


class AIFactory:
    @staticmethod
    def for_settings(settings: Any):
        provider = (getattr(settings, "ai_provider", None) or "bedrock").strip().lower()
        if provider == "gemini":
            from .gemini_client import GeminiClient

            return GeminiClient(settings)
        if provider == "groq":
            from .groq_client import GroqClient

            return GroqClient(settings)
        if provider in ("bedrock", ""):
            from .bedrock_client import BedrockClient

            return BedrockClient(settings)
        raise ValueError(f"Unknown AI_PROVIDER: {provider!r} (supported: bedrock, gemini, groq)")


# Convenience alias so call sites can read like the old import.
def make_ai_client(settings: Any):
    return AIFactory.for_settings(settings)
