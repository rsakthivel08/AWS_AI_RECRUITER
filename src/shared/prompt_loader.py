"""Versioned prompt loader (doc section 6.3).

Each prompt lives in prompts/<name>/vN.md with a sibling vN.schema.json; the
loaded (name, version) pair is recorded on every analysis (NFR-19). Prompt and
code stay in the deployment bundle for the workflow; prod swaps versions by
changing PROMPT_VERSION, which we also write as default in settings.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

PROMPTS_ROOT = Path(__file__).resolve().parent.parent.parent / "prompts"


@lru_cache(maxsize=64)
def load_prompt(name: str, version: str = "v1") -> tuple[str, dict]:
    """Returns (template, schema) for a named, versioned prompt."""
    directory = PROMPTS_ROOT / name
    if not directory.is_dir():
        raise FileNotFoundError(f"Unknown prompt: {name}")
    template_path = directory / f"{version}.md"
    schema_path = directory / f"{version}.schema.json"
    return template_path.read_text(encoding="utf-8"), _json_load(schema_path)


def render(template: str, **values: str) -> str:
    out = template
    for key, value in values.items():
        out = out.replace("{" + key + "}", value)
    return out


def verify_rendered(template: str, values: dict[str, str]) -> str:
    """Like render but raises if any placeholder is left unfilled."""
    out = render(template, **values)
    import re

    leftovers = set(re.findall(r"\{[a-z_]+\}", out))
    leftovers = {p for p in leftovers if p.strip("{}") in values}
    if leftovers:
        raise ValueError(f"Unfilled placeholders: {leftovers}")
    return out


def _json_load(path: Path) -> dict:
    import json

    return json.loads(path.read_text(encoding="utf-8"))


def record_version(name: str, version: str) -> dict[str, str]:
    return {"promptName": name, "promptVersion": version}
