"""Runtime settings.

Values come from Lambda environment variables first (cheap, stable), falling back
to SSM Parameter Store for model IDs that may be swapped without a redeploy
(requirements doc section 3.4 / NFR-13). Unit tests inject env overrides and
never touch SSM.
"""
from __future__ import annotations

import os
from functools import lru_cache
from types import MappingProxyType
from typing import Mapping

DEFAULT_ENV: Mapping[str, str] = MappingProxyType({
    "AWS_REGION": "ap-south-1",
    "TABLE_NAME": "recruiter-dev",
    "TEXT_SOURCE": "freepdf",          # freepdf | textract  (SSM flag, doc deviation)
    "BEDROCK_MODEL_JUDGE": "amazon.nova-pro-v1:0",
    "BEDROCK_MODEL_SMALL": "amazon.nova-micro-v1:0",
    "BEDROCK_MODEL_VISION": "amazon.nova-lite-v1:0",
    "BEDROCK_MODEL_EMBED": "amazon.titan-embed-text-v2:0",
    "EMBED_DIMENSIONS": "3072",
    "GUARDRAIL_ID": "",                # enabled when set (NFR-16)
    "GUARDRAIL_VERSION": "DRAFT",
    "AI_PROVIDER": "gemini",           # bedrock | gemini  (temporary fallback switch)
    "GEMINI_API_KEY": "",              # supplied via env var or SSM SecureString
    "GEMINI_MODEL_TEXT": "gemini-3.5-flash-lite",
    "GEMINI_MODEL_EMBED": "gemini-embedding-001",
    "GROQ_API_KEY": "",                # supplied via env var or SSM SecureString
    "GROQ_MODEL_TEXT": "openai/gpt-oss-20b",
    "GROQ_MAX_TOKENS": "4000",
    "VECTOR_BUCKET": "ar-dev-vectors",
    "VECTOR_INDEX": "resume-vectors",
    "UPLOADS_BUCKET": "ar-dev-uploads",
    "TEXT_BUCKET": "ar-dev-uploads",
    "EXPORTS_BUCKET": "ar-dev-exports",
    "DAILY_MATCH_LIMIT": "500",
    "LIFECYCLE_DAYS": "180",
    "PROMPT_VERSION": "v1",
    "ENV": "dev",
    "FRONTEND_ORIGIN": "",
    "AWS_POWERTOOLS_DISABLED": "1",
})

_SSM_CACHE: dict[str, str] = {}


class Settings:
    """Read-only settings object resolved from env vars (tests) or env+SSM (Lambda)."""

    def __init__(self, env: Mapping[str, str] | None = None):
        merged: dict[str, str] = {}
        for key, default in DEFAULT_ENV.items():
            merged[key] = default
        if env is None:  # real Lambda runtime: defaults + env + SSM overrides
            merged.update({k: v for k, v in os.environ.items() if v})
            merged.update(_ssm_layer())
        else:  # explicit env (unit tests)
            merged.update({k: v for k, v in env.items()})
        self._env = merged

    def __getattr__(self, name: str) -> str:
        key = name.upper()
        if name.startswith("_"):
            raise AttributeError(name)
        if key not in DEFAULT_ENV:
            raise AttributeError(f"Unknown setting: {name}")
        return self._env.get(key, "")

    def int_(self, name: str, default: int = 0) -> int:
        try:
            return int(self._env.get(name.upper()) or default)
        except ValueError:
            return default

    def use_textract(self) -> bool:
        return self._env.get("TEXT_SOURCE", "freepdf") == "textract"


def _ssm_layer() -> dict[str, str]:
    """Fetch model ids / knobs from /ai-recruiter/{env}/* — best effort, cached."""
    path = os.environ.get("SSM_PATH", "")
    if not path or _SSM_CACHE:
        return dict(_SSM_CACHE)
    try:
        import boto3  # local import keeps unit tests SSM-free

        client = boto3.client("ssm", region_name=os.environ.get("AWS_REGION", "ap-south-1"))
        paginator = client.get_paginator("get_parameters_by_path")
        # WithDecryption=True is required for SecureString params (e.g.
        # GEMINI_API_KEY); plain String params are returned unchanged either way.
        for page in paginator.paginate(Path=path, Recursive=True, WithDecryption=True):
            for param in page.get("Parameters", []):
                leaf = param["Name"].rsplit("/", 1)[-1]
                _SSM_CACHE[leaf.upper()] = param["Value"]
    except Exception:  # never break an invocation over configuration
        _SSM_CACHE.clear()
    return dict(_SSM_CACHE)


@lru_cache(maxsize=4)
def settings_for(env_key: str = "runtime") -> Settings:
    return Settings()


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    if env is None:
        return settings_for("runtime")
    return Settings(env)
