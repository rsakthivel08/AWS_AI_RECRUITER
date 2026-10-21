"""AI provider switch: factory selection + Gemini client behaviour (mocked SDK)."""
from __future__ import annotations

from typing import Any

import pytest

from src.shared.ai_factory import AIFactory
from src.shared.settings import Settings


def _settings(**overrides: str) -> Settings:
    base = {"AI_PROVIDER": "bedrock", **overrides}
    return Settings(base)


class _FakeModels:
    """Attribute holder: tests monkeypatch generate_content / embed_content onto it."""

    generate_content: Any = None
    embed_content: Any = None


class _FakeInner:
    models = _FakeModels()


def test_defaults_to_bedrock() -> None:
    from src.shared.bedrock_client import BedrockClient

    assert isinstance(AIFactory.for_settings(_settings()), BedrockClient)


def test_gemini_selected_by_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: dict[str, Any] = {}

    class _FakeClient:
        def __init__(self, api_key: str) -> None:
            calls["api_key"] = api_key

    fake = pytest.importorskip("google.genai")
    monkeypatch.setattr(fake, "Client", _FakeClient)
    client = AIFactory.for_settings(_settings(
        AI_PROVIDER="gemini", GEMINI_API_KEY="k-test"))
    assert calls["api_key"] == "k-test"
    assert type(client).__name__ == "GeminiClient"


def test_gemini_requires_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = pytest.importorskip("google.genai")
    monkeypatch.setattr(fake, "Client", lambda **kw: None)
    with pytest.raises(RuntimeError, match="GEMINI_API_KEY"):
        AIFactory.for_settings(_settings(AI_PROVIDER="gemini"))


def test_unknown_provider_rejected() -> None:
    with pytest.raises(ValueError, match="Unknown AI_PROVIDER"):
        AIFactory.for_settings(_settings(AI_PROVIDER="openai"))


def test_gemini_converse_json_parses(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = pytest.importorskip("google.genai")
    monkeypatch.setattr(fake, "Client", lambda **kw: _FakeInner())
    client = AIFactory.for_settings(_settings(
        AI_PROVIDER="gemini", GEMINI_API_KEY="k-test"))

    class _Resp:
        text = '{"answer": "yes"}'

    monkeypatch.setattr(
        client._client.models, "generate_content", lambda **kw: _Resp())
    out = client.converse_json(system="sys", user="usr")
    assert out == {"answer": "yes"}


def test_gemini_converse_json_retries_on_rate_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = pytest.importorskip("google.genai")
    monkeypatch.setattr(fake, "Client", lambda **kw: _FakeInner())
    client = AIFactory.for_settings(_settings(
        AI_PROVIDER="gemini", GEMINI_API_KEY="k-test"))

    class _Err(Exception):
        pass

    n = {"i": 0}

    class _Resp:
        text = '{"ok": 1}'

    def flaky(**kw: Any) -> Any:
        n["i"] += 1
        if n["i"] == 1:
            raise _Err("429 RESOURCE_EXHAUSTED rate limit")
        return _Resp()

    monkeypatch.setattr(client._client.models, "generate_content", flaky)
    monkeypatch.setattr("time.sleep", lambda s: None)
    assert client.converse_json(system="s", user="u") == {"ok": 1}
    assert n["i"] == 2


def test_gemini_embed_returns_values(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = pytest.importorskip("google.genai")
    monkeypatch.setattr(fake, "Client", lambda **kw: _FakeInner())
    client = AIFactory.for_settings(_settings(
        AI_PROVIDER="gemini", GEMINI_API_KEY="k-test"))

    class _Emb:
        values = [0.1, 0.2]

    class _Resp:
        embedding = _Emb()

    monkeypatch.setattr(
        client._client.models, "embed_content", lambda **kw: _Resp())
    assert client.embed(["hello"]) == [[0.1, 0.2]]
