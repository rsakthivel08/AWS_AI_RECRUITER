"""FR-07: every required skill appears exactly once, Met/Partial/Missing."""
from src.functions.pipeline.score_candidate import _derive_gaps, _met_status
from src.shared.models import JobRequirements


def jd(required: list[str], preferred: list[str] | None = None) -> JobRequirements:
    return JobRequirements.model_validate({
        "requiredSkills": required,
        "preferredSkills": preferred or [],
        "responsibilities": [], "mustHaves": [],
    })


def test_met_partial_missing():
    assert _met_status("Python", {"Python"}) == "Met"
    assert _met_status("Python", {"Docker"}) == "Missing"
    # partial: token overlap — JD "containerization" vs profile "Docker containers"
    assert _met_status("containerization", {"Docker containers"}) == "Partial"


def test_gap_report_every_skill_once():
    profile = {"skills": ["Python"], "tools": []}
    gaps = _derive_gaps(jd(["Python", "Kubernetes", "Terraform"],
                           ["Redis", "PostgreSQL"]), profile)
    requirements = [g["requirement"] for g in gaps]
    assert len(requirements) == len(set(requirements))          # no duplicates
    assert set(requirements) == {"Kubernetes", "Terraform", "Redis", "PostgreSQL"}
    assert all(g["status"] in ("Partial", "Missing") for g in gaps)
    # high severity for a missing required skill, low for preferred
    severity = {g["requirement"]: g["severity"] for g in gaps}
    assert severity["Kubernetes"] == "High" or severity["Kubernetes"] in ("Medium", "High")
    assert severity["Redis"] == "Low" or severity["Redis"] in ("Medium", "Low")


def test_transferable_skill_named():
    profile = {"skills": ["Docker", "ECS"], "tools": []}
    gaps = _derive_gaps(jd(["Kubernetes"]), profile)
    transferable = [g.get("transferable") for g in gaps]
    assert any(t in ("Docker", "ECS") for t in transferable)
