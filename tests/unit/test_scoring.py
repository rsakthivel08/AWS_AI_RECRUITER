"""FR-06 invariant: total equals the weighted sum of criterion scores."""
import pytest

from src.shared.keys import DEFAULT_RUBRIC, compute_weighted_total, rank_sort_key, score_band, validate_rubric


def test_total_is_weighted_sum():
    criteria = [
        {"name": "a", "weight": 35, "score": 80, "evidence": []},
        {"name": "b", "weight": 25, "score": 40, "evidence": []},
        {"name": "c", "weight": 40, "score": 100, "evidence": []},
    ]
    # (80*35 + 40*25 + 100*40) / 100 = 28 + 10 + 40 = 78
    assert compute_weighted_total(criteria) == 78


def test_total_zero_weights_returns_zero():
    assert compute_weighted_total([{"name": "a", "weight": 0, "score": 50, "evidence": []}]) == 0


def test_default_rubric_sums_to_100():
    assert sum(DEFAULT_RUBRIC.values()) == 100
    validate_rubric(DEFAULT_RUBRIC)          # must not raise


def test_rubric_validation_rejects_bad_sums():
    with pytest.raises(ValueError):
        validate_rubric({**DEFAULT_RUBRIC, "requiredSkills": 30})
    with pytest.raises(ValueError):
        validate_rubric({**DEFAULT_RUBRIC, "seniority": -1})


def test_score_bands():
    assert score_band(90) == "Strong"
    assert score_band(75) == "Strong"
    assert score_band(55) == "Good"
    assert score_band(35) == "Partial"
    assert score_band(10) == "Weak"


def test_rank_sort_key_descending():
    """score 100 -> '0000' ... 0 -> '0100'; ascending = best rank first."""
    keys = [rank_sort_key(s) for s in (100, 75, 55, 35, 0)]
    assert keys == ["0000", "0025", "0045", "0065", "0100"]
    assert keys == sorted(keys)  # ascending string order = descending score
