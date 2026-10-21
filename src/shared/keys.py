"""Key builders and scoring maths (doc sections 3.3 / 4.2)."""
from __future__ import annotations

from typing import Iterable

# ---- DynamoDB single-table keys ------------------------------------------------

def user_pk(sub: str) -> str:
    return f"U#{sub}"

def cand_sk(cid: str) -> str:
    return f"C#{cid}"

def job_sk(jid: str) -> str:
    return f"J#{jid}"

def analysis_sk(jid: str, cid: str) -> str:
    return f"A#{jid}#{cid}"

def role_sk(cid: str) -> str:
    return f"R#{cid}"

def gsi_rank_pk(sub: str, jid: str) -> str:
    return f"{user_pk(sub)}#J#{jid}"

def gsi_dedup_pk(sub: str, sha256: str) -> str:
    return f"{user_pk(sub)}#H#{sha256}"

def rank_sort_key(score: int) -> str:
    """Zero-padded descending-sort key: score 100 -> '0000', 0 -> '1000'.

    GSI1SK sorts ascending, so smaller key = better rank.
    """
    return f"{100 - max(0, min(100, int(score))):04d}"


# ---- Rubric (FR-05 to FR-08) ----------------------------------------------------

DEFAULT_RUBRIC: dict[str, int] = {
    "requiredSkills": 35,
    "relevantExperience": 25,
    "seniority": 10,
    "preferredSkills": 10,
    "educationCerts": 10,
    "domainFit": 10,
}


def validate_rubric(weights: dict[str, int]) -> None:
    if abs(sum(weights.values()) - 100) > 0:
        raise ValueError("Rubric weights must sum to 100")
    if any(int(w) < 0 for w in weights.values()):
        raise ValueError("Rubric weights must be non-negative")


def compute_weighted_total(criteria: Iterable[dict]) -> int:
    """Total score = weighted mean of criterion scores, computed in code (never by the LLM)."""
    criteria = list(criteria)
    weight_sum = sum(int(c["weight"]) for c in criteria)
    if weight_sum <= 0:
        return 0
    total = sum(int(c["score"]) * int(c["weight"]) for c in criteria)
    return round(total / weight_sum)


def score_band(score: int) -> str:
    if score >= 75:
        return "Strong"
    if score >= 55:
        return "Good"
    if score >= 35:
        return "Partial"
    return "Weak"
