"""Bedrock access via the Converse API (doc section 3.4).

All generation goes through converse_json: system-led, delimited untrusted text,
temperature 0, JSON-schema validated, retry with backoff on throttle/service
errors. Embeddings use Titan Text Embeddings V2 (InvokeModel). Guardrails are
attached when configured (NFR-16).
"""
from __future__ import annotations

import json
import time
from typing import Any

import boto3

from .logging import get_logger
from .models import parse_model_json

LOG = get_logger("bedrock")

RETRYABLE = ("ThrottlingException", "ServiceUnavailableException", "InternalServerException",
             "TooManyRequestsException", "BedrockThrottledException")


class BedrockClient:
    def __init__(self, settings: Any):
        self.settings = settings
        self._client = boto3.client("bedrock-runtime", region_name=settings.aws_region)
        self.guardrail_id = settings.guardrail_id or None
        self.guardrail_version = getattr(settings, "guardrail_version", "DRAFT")

    # ---------- generation -------------------------------------------------------

    def converse_json(self, system: str, user: str, model_id: str, temperature: float = 0.0,
                      max_tokens: int = 4000, retries: int = 3) -> dict:
        """Call a Bedrock model and parse the JSON it must return."""
        last_error: Exception | None = None
        for attempt in range(retries):
            try:
                request: dict[str, Any] = {
                    "modelId": model_id,
                    "system": [{"text": system}],
                    "messages": [{"role": "user", "content": [{"text": user}]}],
                    "inferenceConfig": {"temperature": temperature, "topP": 0.9 if temperature else 1,
                                        "maxTokens": max_tokens},
                }
                if self.guardrail_id:
                    request["guardrailConfig"] = {
                        "guardrailIdentifier": self.guardrail_id,
                        "guardrailVersion": self.guardrail_version,
                        "trace": "enabled",
                    }
                resp = self._client.converse(**request)
                text = "".join(
                    block.get("text", "")
                    for block in resp["output"]["message"]["content"]
                )
                return parse_model_json(text)
            except Exception as exc:  # noqa: BLE001 - single retry policy
                if RETRYABLE and any(name in type(exc).__name__ for name in RETRYABLE):
                    wait = min(2 ** attempt, 8)
                    LOG.warning("bedrock retryable error", extra={"extra_fields": {
                        "error": type(exc).__name__, "attempt": attempt + 1, "backoff": wait}})
                    last_error = exc
                    time.sleep(wait)
                    continue
                raise
        raise RuntimeError(f"Bedrock call failed after {retries} retries") from last_error

    # ---------- embeddings ---------------------------------------------------------

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Titan Text Embeddings V2 (1024 dims, normalised)."""
        vectors: list[list[float]] = []
        for text in texts:
            body = {
                "inputText": text[:8000],  # model cap safety
                "dimensions": int(self.settings.embed_dimensions or 1024),
                "normalize": True,
                "embeddingTypes": ["float"],
            }
            resp = self._client.invoke_model(
                modelId=self.settings.bedrock_model_embed,
                body=json.dumps(body),
                contentType="application/json",
                accept="application/json",
            )
            payload = json.loads(resp["body"].read())
            vectors.append(payload["embedding"])
        return vectors

    # ---------- vision fallback (PDF pages without a text layer) -------------------

    def converse_vision(self, prompt: str, image_bytes: bytes,
                        media_type: str = "image/png") -> str:
        request = {
            "modelId": self.settings.bedrock_model_vision,
            "messages": [{
                "role": "user",
                "content": [
                    {"image": {"format": media_type.split("/")[-1].upper(),
                               "source": {"bytes": image_bytes}}},
                    {"text": prompt},
                ],
            }],
            "inferenceConfig": {"temperature": 0.0, "maxTokens": 2000},
        }
        resp = self._client.converse(**request)
        return "".join(block.get("text", "") for block in resp["output"]["message"]["content"])
