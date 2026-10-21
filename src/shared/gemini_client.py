"""Google Gemini fallback provider (temporary while the Bedrock account hold persists).

Implements the SAME interface as ``BedrockClient`` — ``converse_json``, ``embed``,
``converse_vision`` — so
``AIFactory.for_settings(settings)`` can hand either one to every call site with
zero changes elsewhere. Selected by the ``AI_PROVIDER`` setting
(``bedrock`` — default — | ``gemini``).

Gemini specifics:
- Structured generation: ``response_json_schema`` forces JSON matching a plain
  (not Pydantic) schema; we pass the app's Pydantic-derived schema shapes only
  where the contract needs them, otherwise the same prompt text as Bedrock is
  used and the reply is parsed with the same defensive ``parse_model_json``.
- Embeddings: ``text-embedding-004`` produces 768-dim vectors (normalised) —
  dimension differs from Titan V2 (1024), which forces re-upload/re-vectorise
  of the corpus when switching providers; that's OK for this dev deployment.
- No vision: ``converse_vision`` raises unless a Gemini multimodal model id is
  configured — the freepdf text path never calls it (doc deviation note).
"""
from __future__ import annotations

import time
from typing import Any

from .logging import get_logger
from .models import parse_model_json

LOG = get_logger("gemini")

DEFAULT_GEMINI_TEXT = "gemini-3.5-flash-lite"
DEFAULT_GEMINI_EMBED = "gemini-embedding-001"


FALLBACK_TEXT_MODELS = [
    "gemini-3.5-flash-lite",
    "gemini-3.1-flash-lite",
    "gemini-2.5-flash-lite",
    "gemini-2.5-flash",
    "gemini-3.8-flash",
]


class GeminiClient:
    def __init__(self, settings: Any):
        from google import genai  # bundled via the lambda layer / requirements

        api_key = (getattr(settings, "gemini_api_key", None) or "").strip()
        if not api_key:
            raise RuntimeError(
                "AI_PROVIDER=gemini but GEMINI_API_KEY is not set (SSM: GEMINI_API_KEY)")
        self.settings = settings
        self._text_model = (getattr(settings, "gemini_model_text", None) or DEFAULT_GEMINI_TEXT).strip()
        self._embed_model = (getattr(settings, "gemini_model_embed", None) or DEFAULT_GEMINI_EMBED).strip()
        # gemini-embedding-001 emits 3072 dims by default; honor EMBED_DIMENSIONS
        # so vectors stay comparable across providers (and fit any S3 Vectors index).
        self._embed_dims = int(getattr(settings, "embed_dimensions", None) or 0)
        self._client = genai.Client(api_key=api_key)

    # ---------- generation -------------------------------------------------------

    def converse_json(self, system: str, user: str, model_id: str = "",
                      temperature: float = 0.0, max_tokens: int = 4000,
                      retries: int = 2) -> dict:
        _unused = model_id  # interface parity; the Gemini text model is a setting
        models = self._client.models
        models_to_try = [self._text_model] + [m for m in FALLBACK_TEXT_MODELS if m != self._text_model]
        last_error: Exception | None = None

        for cur_model in models_to_try:
            for attempt in range(retries):
                try:
                    resp = models.generate_content(
                        model=cur_model,
                        contents=f"{system}\n\n---\n\n{user}",
                        config={
                            "temperature": temperature,
                            "max_output_tokens": max_tokens,
                            "response_mime_type": "application/json",
                        },
                    )
                    return parse_model_json(resp.text or "")
                except Exception as exc:  # noqa: BLE001
                    last_error = exc
                    msg = str(exc).lower()
                    if any(t in msg for t in ("resource_exhausted", "quota", "429", "404", "503", "unavailable", "overloaded", "deadline")):
                        LOG.warning("gemini model unavailable/quota exhausted, falling back to next model",
                                    extra={"extra_fields": {"model": cur_model, "error": str(exc)[:200]}})
                        break  # try next model in models_to_try
                    if _retryable(exc):
                        wait = min(2 ** attempt, 4)
                        LOG.warning("gemini retryable error", extra={"extra_fields": {
                            "error": type(exc).__name__, "attempt": attempt + 1, "backoff": wait}})
                        time.sleep(wait)
                        continue
                    raise
        raise RuntimeError(f"Gemini call failed on all models: {last_error}") from last_error


    # ---------- embeddings ---------------------------------------------------------

    def embed(self, texts: list[str]) -> list[list[float]]:
        models = self._client.models
        vectors: list[list[float]] = []
        for text in texts:
            config: dict[str, Any] = {"task_type": "RETRIEVAL_DOCUMENT"}
            if self._embed_dims in (768, 1536, 3072):  # model-supported output sizes
                config["output_dimensionality"] = self._embed_dims
            resp = models.embed_content(
                model=self._embed_model,
                contents=text[:8000],
                config=config,
            )
            # response.embedding is a ContentEmbedding; values is the vector.
            emb = getattr(resp, "embedding", None) or (
                resp.embeddings[0] if getattr(resp, "embeddings", None) else None)
            values = list(emb.values) if emb is not None else []
            if not values:
                raise RuntimeError("Gemini embed returned no values")
            vectors.append(values)
        return vectors

    # ---------- vision ---------------------------------------------------------------

    def converse_vision(self, prompt: str, image_bytes: bytes,
                        media_type: str = "image/png") -> str:
        """Multimodal PDF-page reading, mirroring BedrockClient.converse_vision."""
        from google.genai import types as gtypes

        mime = media_type or "image/png"
        resp = self._client.models.generate_content(
            model=self._text_model,
            contents=[
                gtypes.Part.from_bytes(data=image_bytes, mime_type=mime),
                prompt,
            ],
            config={"temperature": 0.0, "max_output_tokens": 2000},
        )
        return resp.text or ""


def _retryable(exc: Exception) -> bool:
    name = type(exc).__name__.lower()
    msg = str(exc).lower()
    hints = ("resource_exhausted", "rate", "_quota", "unavailable", "internal",
             "deadline", "temporarily")
    return any(h in name or h in msg for h in hints)
