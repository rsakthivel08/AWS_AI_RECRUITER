"""Schema enforcement tests: model outputs must validate or the step fails (NFR-16)."""
import pytest
from pydantic import ValidationError

from src.shared.models import (
    Analysis,
    ClaimsOut,
    InconsistenciesOut,
    ParseSearchOut,
    Profile,
    QuestionsOut,
    ScoreCriteriaOut,
    parse_model_json,
)


def test_profile_fields_default_to_empty():
    p = Profile.model_validate({"skills": ["Python"]})
    assert p.skills == ["Python"]
    assert p.tools == [] and p.experience == [] and p.claims == []


def test_score_criteria_rejects_out_of_range():
    with pytest.raises(ValidationError):
        ScoreCriteriaOut.model_validate({
            "criteria": [{"name": "x", "weight": 35, "score": 140, "evidence": []}]})


def test_gap_status_limited_to_enum():
    with pytest.raises(ValidationError):
        ClaimsOut.model_validate({"claimChecks": [{"claim": "c", "verdict": "Unknown"}]})


def test_inconsistency_flag_types():
    ok = InconsistenciesOut.model_validate({"flags": [{"type": "gap", "detail": "no jobs Mar-Nov"}]})
    assert ok.flags[0].type == "gap"
    with pytest.raises(ValidationError):
        InconsistenciesOut.model_validate({"flags": [{"type": "wild", "detail": "x"}]})
    with pytest.raises(ValidationError):    # type is a required field
        InconsistenciesOut.model_validate({"flags": [{"detail": "x only"}]})


def test_screening_questions_bounds():
    with pytest.raises(ValidationError):
        QuestionsOut.model_validate({"questions": []})   # schema requires 3-12


def test_search_filters_shape():
    f = ParseSearchOut.model_validate({"skills": ["Python"], "minYears": 3,
                                       "semanticQuery": "backend python"})
    assert f.minYears == 3 and f.keywords == [] and f.domain is None


def test_analysis_store_shape():
    a = Analysis(
        candidateId="c_1", jobId="j_1", totalScore=72,
        criteria=[{"name": "requiredSkills", "weight": 35, "score": 80, "evidence": [
            {"requirement": "CI/CD", "quote": "Built CI/CD with GitHub Actions"}]}],
        modelId="amazon.nova-pro-v1", promptVersion="v1",
    )
    assert a.criteria[0].evidence[0].quote.startswith("Built")


def test_parse_model_json_extracts_object_from_chatter():
    text = 'Sure! Here is your JSON:\n{"skills": ["Python"]}\nLet me know if you need more.'
    assert parse_model_json(text) == {"skills": ["Python"]}
    with pytest.raises(ValueError):
        parse_model_json("no json here at all")
