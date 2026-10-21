"""Groq LLM provider — OpenAI-compatible JSON chat completions.

Same interface as ``BedrockClient``/``GeminiClient`` — ``converse_json``, ``embed``,
``converse_vision`` — so ``AIFactory.for_settings(settings)`` can hand this to every
call site unchanged. Selected by the ``AI_PROVIDER`` setting: ``groq``.

Groq specifics:
- Generation: chat/completions endpoint with a strict-JSON system instruction;
  parsed defensively by the same ``parse_model_json`` used by the other providers.
- Embeddings: NOT supported by Groq — vectors come from the Bedrock Titan path
  regardless of AI_PROVIDER (VectorStore keeps using the Bedrock embed client).
- Vision: gpt-oss-20b is text-only; extract_text's vision fallback is not used by
  the freepdf path, matching the GeminiClient deviation note.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any

from .logging import get_logger
from .models import parse_model_json

LOG = get_logger("groq")

GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"
DEFAULT_GROQ_TEXT = "openai/gpt-oss-20b"

RETRYABLE_HINTS = ("rate limit", "429", "503", "overloaded", "timeout", "temporarily")


class GroqClient:
    def __init__(self, settings: Any):
        api_key = (getattr(settings, "groq_api_key", None) or "").strip()
        if not api_key:
            raise RuntimeError(
                "AI_PROVIDER=groq but GROQ_API_KEY is not set (SSM: GROQ_API_KEY)")
        self.settings = settings
        self._text_model = (getattr(settings, "groq_model_text", None) or DEFAULT_GROQ_TEXT).strip()
        self._max_tokens = int(getattr(settings, "groq_max_tokens", None) or 4000)
        self._api_key = api_key

    # ---------- generation -------------------------------------------------------

    def converse_json(self, system: str, user: str, model_id: str = "",
                      temperature: float = 0.0, max_tokens: int = 4000,
                      retries: int = 3) -> dict:
        _unused = model_id  # interface parity; the Groq text model is a setting
        payload = {
            "model": self._text_model,
            "temperature": temperature,
            "max_tokens": min(max_tokens, self._max_tokens),
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        last_error: Exception | None = None
        for attempt in range(retries):
            wait = 0.0
            try:
                req = urllib.request.Request(
                    GROQ_API_URL,
                    data=json.dumps(payload).encode("utf-8"),
                    headers={
                        "Content-Type": "application/json",
                        "Authorization": f"Bearer {self._api_key}",
                        # Groq's edge rejects UA-less requests (HTTP 403 err 1010);
                        # bare urllib sends no User-Agent by default.
                        "User-Agent": "ai-recruiter-api/1.0",
                        "Accept": "application/json",
                    },
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=60) as resp:
                    body = json.loads(resp.read().decode("utf-8"))
                content = body.get("choices", [{}])[0].get("message", {}).get("content", "")
                if body.get("choices") and body["choices"][0].get("finish_reason") == "length":
                    LOG.warning("groq completion truncated", extra={"extra_fields": {
                        "model": self._text_model, "max_tokens": payload["max_tokens"]}})
                return parse_model_json(content)
            except urllib.error.HTTPError as exc:
                last_error = exc
                if exc.code == 429:
                    # Free tier is 30 RPM / 8k TPM: the token bucket refills per
                    # minute, so honour the provider's reset window instead of a
                    # bare exponential backoff that retries into the same empty
                    # bucket. Fall back to the wait header when absent.
                    wait = self._rate_limit_wait(exc) or (20.0 * (attempt + 1))
                    if attempt < retries - 1:
                        LOG.warning("groq rate limited", extra={"extra_fields": {
                            "attempt": attempt + 1, "backoff": wait}})
                        time.sleep(wait)
                        continue
                    # Retries exhausted while rate limited: finish this call on
                    # Gemini so a match run degrades to slower instead of FAILED.
                    LOG.warning("groq rate limit exhausted; falling back to gemini",
                                extra={"extra_fields": {"model": self._text_model}})
                    return self._gemini_fallback().converse_json(
                        system=system, user=user, temperature=temperature,
                        max_tokens=max_tokens)
                raise
            except Exception as exc:  # noqa: BLE001 - single retry policy
                last_error = exc
                msg = str(exc).lower()
                if any(h in msg for h in RETRYABLE_HINTS):
                    wait = min(2 ** attempt, 8)
                    LOG.warning("groq retryable error", extra={"extra_fields": {
                        "error": type(exc).__name__, "attempt": attempt + 1, "backoff": wait}})
                    time.sleep(wait)
                    continue
                raise
        raise RuntimeError(f"Groq call failed after {retries} retries: {last_error}")

    # ---------- rate limiting ------------------------------------------------------

    @staticmethod
    def _rate_limit_wait(exc: urllib.error.HTTPError) -> float | None:
        """Wait time until the token bucket refills, from Groq's reset headers.

        Groq reports x-ratelimit-reset-requests / x-ratelimit-reset-tokens as
        durations ("1m29s", "2.5s", "59m"); Retry-After is not always present.
        """
        candidates = []
        for name in ("retry-after", "x-ratelimit-reset-tokens", "x-ratelimit-reset-requests"):
            raw = exc.headers.get(name)
            if not raw:
                continue
            try:
                candidates.append(float(raw))
                continue
            except ValueError:
                pass
            total, num = 0.0, ""
            for ch in raw:
                if ch.isdigit() or ch == ".":
                    num += ch
                else:
                    if num:
                        value = float(num)
                        total += value * 60 if ch in "hm" else value
                    num = ""
            if num:
                total += float(num)
            if total > 0:
                candidates.append(total)
        return min(candidates) if candidates else None

    def _gemini_fallback(self):
        from .ai_factory import AIFactory

        settings = self.settings
        provider = (getattr(settings, "ai_provider", None) or "").lower()
        if provider == "groq":
            # Temporarily view settings as gemini so the factory returns the
            # Gemini client (monkey-patch-free, read-only attribute).
            class _GeminiView:
                def __getattr__(self, name: str):
                    return getattr(settings, name)

                ai_provider = "gemini"

            return AIFactory.for_settings(_GeminiView())
        return AIFactory.for_settings(settings)

    # ---------- embeddings (not supported — delegated to Bedrock) -----------------

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Groq has no embeddings API — route vectors through the Gemini client.

        Bedrock InvokeModel is currently not permitted on this account (the
        original reason for the Gemini fallback), so vectors must not be
        delegated there. Existing Gemini vectors stay comparable — the corpus
        was vectorised with gemini-embedding-001 at the same dimensions.
        """
        from .gemini_client import GeminiClient

        LOG.debug("groq embed fallback to gemini embedding", extra={"extra_fields": {"n": len(texts)}})
        return GeminiClient(self.settings).embed(texts)

    # ---------- vision -------------------------------------------------------------

    def converse_vision(self, prompt: str, image_bytes: bytes,
                        media_type: str = "image/png") -> str:
        raise NotImplementedError(
            "gpt-oss-20b is text-only; vision extraction must use the Bedrock vision model")
