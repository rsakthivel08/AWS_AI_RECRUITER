"""Pydantic models for profiles, JDs, analyses and API payloads.

These are also enforced as Bedrock output schemas (requirements doc section 4.2,
NFR-16): the model must return JSON matching these shapes, otherwise the workflow
step retries once and then fails.
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator


def _clean_str_list(v: Any) -> list[str]:
    if not isinstance(v, list):
        return []
    result = []
    for item in v:
        if isinstance(item, str):
            s = item.strip()
            if s:
                result.append(s)
        elif isinstance(item, dict):
            if "name" in item:
                val = str(item["name"])
                if item.get("date"):
                    val += f" ({item['date']})"
                result.append(val)
            elif "language" in item:
                val = str(item["language"])
                if item.get("proficiency"):
                    val += f" ({item['proficiency']})"
                result.append(val)
            elif "skill" in item:
                result.append(str(item["skill"]))
            elif "tool" in item:
                result.append(str(item["tool"]))
            else:
                parts = [str(x) for x in item.values() if x and isinstance(x, (str, int, float))]
                if parts:
                    result.append(" - ".join(parts))
        elif item is not None:
            result.append(str(item))
    return result


# ---------- Candidate profile (FR-03) ------------------------------------------

class Experience(BaseModel):
    title: str = "Professional"
    company: str | None = None
    start: str | None = None
    end: str | None = None
    summary: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _coerce_exp(cls, data: Any) -> Any:
        if isinstance(data, dict):
            if not data.get("title"):
                data["title"] = data.get("role") or data.get("position") or "Professional"
        return data


class Education(BaseModel):
    degree: str = "Degree"
    institution: str | None = None
    year: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _coerce_edu(cls, data: Any) -> Any:
        if isinstance(data, dict):
            if not data.get("degree"):
                data["degree"] = data.get("name") or data.get("field") or "Degree"
        return data


class Project(BaseModel):
    name: str = "Project"
    summary: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _coerce_proj(cls, data: Any) -> Any:
        if isinstance(data, dict):
            if not data.get("name"):
                data["name"] = data.get("title") or "Project"
        return data


class Profile(BaseModel):
    name: str | None = None
    email: str | None = None
    phone: str | None = None
    location: str | None = None
    summary: str | None = None
    skills: list[str] = []
    tools: list[str] = []
    languages: list[str] = []
    experience: list[Experience] = []
    education: list[Education] = []
    projects: list[Project] = []
    certifications: list[str] = []
    claims: list[str] = []

    @field_validator("skills", "tools", "languages", "certifications", "claims", mode="before")
    @classmethod
    def _clean_lists(cls, v: Any) -> list[str]:
        return _clean_str_list(v)


# ---------- Job description (FR-04) --------------------------------------------

class JobRequirements(BaseModel):
    title: str | None = None
    requiredSkills: list[str] = []
    preferredSkills: list[str] = []
    minYears: float | None = None
    seniority: str | None = None
    education: str | None = None
    domain: str | None = None
    responsibilities: list[str] = []
    certificationsRequired: list[str] = []
    mustHaves: list[str] = []          # e.g. mandatory certification -> flag when missing


# ---------- Matching output (FR-05 to FR-12) ------------------------------------

Verdict = Literal["Supported", "Weak", "Unsupported"]
GapStatus = Literal["Met", "Partial", "Missing"]


class Evidence(BaseModel):
    requirement: str
    quote: str


class CriterionScore(BaseModel):
    name: str
    weight: int
    score: int = Field(ge=0, le=100)
    evidence: list[Evidence] = []      # empty means "no evidence found"


class Gap(BaseModel):
    requirement: str
    status: GapStatus
    severity: Literal["Low", "Medium", "High"] = "Medium"
    transferable: str | None = None


class ClaimCheck(BaseModel):
    claim: str
    verdict: Verdict
    support: str | None = None


class Flag(BaseModel):
    model_config = {"extra": "forbid"}
    type: Literal["gap", "overlap", "implausible", "seniority",
                  "claim", "inconsistency", "mustHave"]
    detail: str


class ScreeningQuestion(BaseModel):
    model_config = {"extra": "forbid"}
    category: str                      # gap | weakClaim | inconsistency | general
    question: str
    why: str
    goodAnswer: str


class Analysis(BaseModel):
    candidateId: str
    jobId: str
    totalScore: int
    criteria: list[CriterionScore]
    gaps: list[Gap] = []
    claimChecks: list[ClaimCheck] = []
    flags: list[Flag] = []
    questions: list[ScreeningQuestion] = []
    modelId: str
    promptVersion: str
    createdAt: str | None = None       # set by the pipeline, not the model


# ---------- Search / Q&A (FR-12, FR-13) -----------------------------------------

class SearchFilters(BaseModel):
    semanticQuery: str | None = None
    skills: list[str] = []
    minYears: float | None = None
    seniority: str | None = None
    domain: str | None = None
    keywords: list[str] = []


from pydantic import BaseModel, Field, model_validator


class Citation(BaseModel):
    quote: str = ""
    chunkIndex: int = Field(-1, ge=-1)


# ---------- Prompt-response envelopes (model-facing schemas) -------------------

class ProfileOut(Profile):
    """Model returns a Profile plus source snippets per claims/term usage."""
    snippets: list[str] = Field(default_factory=list, description="verbatim source snippets")


class ScoreCriteriaOut(BaseModel):
    model_config = {"extra": "forbid"}
    criteria: list[CriterionScore] = Field(min_length=1)


class GapsOut(BaseModel):
    gaps: list[Gap]


class ClaimsOut(BaseModel):
    model_config = {"extra": "forbid"}
    claimChecks: list[ClaimCheck]


class InconsistenciesOut(BaseModel):
    model_config = {"extra": "forbid"}
    flags: list[Flag]


class QuestionsOut(BaseModel):
    model_config = {"extra": "forbid"}
    questions: list[ScreeningQuestion] = Field(min_length=3, max_length=12)


class ParseSearchOut(SearchFilters):
    """parse_search_query output is a structured-filter + semantic query pair."""
    pass


class RationaleOut(BaseModel):
    rationale: str


class CompareOut(BaseModel):
    summary: str
    strengths: dict[str, list[str]] = Field(default_factory=dict)


class AnswerOut(BaseModel):
    answer: str = ""
    citations: list[Citation] = []
    missingInfo: bool = False

    @model_validator(mode="before")
    @classmethod
    def _coerce_answer(cls, data: Any) -> Any:
        if isinstance(data, str):
            return {"answer": data, "citations": [], "missingInfo": False}
        if isinstance(data, dict):
            if "answer" not in data or not data["answer"]:
                for key in ("response", "result", "text", "summary", "message", "explanation"):
                    if key in data and data[key]:
                        data["answer"] = str(data[key])
                        break
                else:
                    str_vals = [f"{k}: {v}" for k, v in data.items() if k not in ("citations", "missingInfo") and v]
                    if str_vals:
                        data["answer"] = "\n".join(str_vals)
                    else:
                        data["answer"] = "No answer provided."
            if "citations" not in data or not isinstance(data["citations"], list):
                data["citations"] = []
            else:
                clean_cits = []
                for c in data["citations"]:
                    if isinstance(c, str):
                        clean_cits.append({"quote": c, "chunkIndex": -1})
                    elif isinstance(c, dict):
                        clean_cits.append(c)
                data["citations"] = clean_cits
        return data


def parse_model_json(text: str) -> Any:
    """Extract the first JSON object/array from model text (defensive against chatter)."""
    import json
    import re

    if not text or not isinstance(text, str):
        raise ValueError("Model response did not contain parsable JSON")

    cleaned = text.strip()
    # Strip markdown code blocks if wrapped
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
        cleaned = cleaned.strip()

    # Try direct parse
    try:
        return json.loads(cleaned)
    except Exception:
        pass

    # Find matching outermost braces/brackets
    for opener, closer in (("{", "}"), ("[", "]")):
        start = cleaned.find(opener)
        end = cleaned.rfind(closer)
        if start != -1 and end > start:
            candidate = cleaned[start:end + 1]
            try:
                return json.loads(candidate)
            except json.JSONDecodeError:
                # Remove trailing commas before closing braces/brackets
                fixed = re.sub(r",\s*([\]}])", r"\1", candidate)
                try:
                    return json.loads(fixed)
                except Exception:
                    continue
    raise ValueError("Model response did not contain parsable JSON")

