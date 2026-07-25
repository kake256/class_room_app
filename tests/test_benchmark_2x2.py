import pytest

from grader.benchmark_2x2 import (
    MODELS, RubricBatchGrader, _batch_schema, _combine, _consolidate_scores, _metrics,
    _visual_batches,
)


def test_models_include_minicpm_comparison_role():
    assert MODELS["minicpm"] == ("minicpm-v45", "openbmb/MiniCPM-V-4_5")


def test_visual_batches_bound_images_and_items():
    items = [
        {"available_pages": 4}, {"available_pages": 5},
        {"available_pages": 3}, {"available_pages": 1},
    ]
    batches = _visual_batches(items, max_items=3, max_images=10)
    assert [len(batch) for batch in batches] == [2, 2]
    assert all(sum(item["available_pages"] for item in batch) <= 10 for batch in batches)


def test_consolidation_and_anonymous_metrics():
    assert _consolidate_scores([3, 3]) == (3, False)
    assert _consolidate_scores([3, 2]) == (2, True)
    with pytest.raises(ValueError):
        _consolidate_scores([])
    metrics = _metrics({"A": 3, "B": 1, "C": 0}, {"A": 2, "B": 1, "C": 2})
    assert metrics["n"] == 3
    assert metrics["exact_rate"] == pytest.approx(1 / 3, abs=0.0001)
    assert metrics["within_one_rate"] == pytest.approx(2 / 3, abs=0.0001)
    assert metrics["mean_difference"] == pytest.approx(-1 / 3, abs=0.0001)
    assert metrics["difference_distribution"] == {"-2": 1, "0": 1, "1": 1}


def test_batch_validation_and_schema_require_every_item():
    schema = _batch_schema(["A", "B"])
    assert schema["properties"]["results"]["minItems"] == 2
    grade = {
        "gate": {"pass": True, "reason": "ok"},
        "criteria": [
            {"name": name, "score": 1, "evidence": "e", "comment": "c"}
            for name in ("quantitative", "method", "discussion")
        ],
        "total": 3, "flags": [], "notable": None,
    }
    value = {"results": [
        {"item_id": "A", "grade": grade}, {"item_id": "B", "grade": grade},
    ]}
    assert set(RubricBatchGrader._validate(value, ["A", "B"])) == {"A", "B"}
    with pytest.raises(ValueError):
        RubricBatchGrader._validate(
            {"results": [{"item_id": "A", "grade": grade}]}, ["A", "B"])


def test_pre_pairwise_combination_matches_legacy_priority():
    primary = {
        "scores": {"A": 3, "B": 3, "C": 1, "D": 2},
        "inconsistent": {"A": False, "B": False, "C": False, "D": True},
    }
    judge = {
        "scores": {"A": 3, "B": 2, "C": 2, "D": 0},
        "crit_min_sum": {"A": 3, "B": 1.5, "C": 2, "D": 0},
    }
    assert _combine(primary, judge, 2.0) == {"A": 3, "B": 2, "C": 2, "D": 2}
