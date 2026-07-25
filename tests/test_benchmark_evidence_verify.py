import pytest

from grader.benchmark_evidence_verify import (
    _comparison, _risk_reasons, _validate_grade, _validate_verification,
)


def test_validate_grade_requires_score_level_reason_and_evidence_shape():
    value = {"results": [{
        "item_id": "A", "internal_score": 2, "rubric_level": "2",
        "confidence": .9, "reason": "基準を満たす", "evidence": "答案中の根拠",
    }]}
    assert _validate_grade(value, ["A"])["A"]["internal_score"] == 2
    bad = {"results": [{**value["results"][0], "rubric_level": "4"}]}
    with pytest.raises(ValueError):
        _validate_grade(bad, ["A"])


def test_verification_schema_has_no_score_and_validation_rejects_missing_checks():
    value = {"results": [{
        "item_id": "A", "evidence_exists": True, "criterion_consistent": True,
        "visual_coverage_ok": True, "boundary": False, "confidence": .8,
        "issues": [],
    }]}
    result = _validate_verification(value, ["A"])["A"]
    assert "internal_score" not in result
    with pytest.raises(ValueError):
        _validate_verification({"results": [{**value["results"][0], "boundary": 0}]}, ["A"])


def test_risk_reasons_include_failed_checks_boundary_confidence_and_issues():
    item = {"status": "visual_required", "available_pages": 9, "total_pages": 9}
    verification = {
        "evidence_exists": False, "criterion_consistent": True,
        "visual_coverage_ok": False, "boundary": True, "confidence": .6,
        "issues": ["図表の確認不足"],
    }
    assert _risk_reasons(
        item, {"internal_score": 2}, verification,
        max_visual_pages=8, confidence_threshold=.6) == [
            "evidence_exists_false", "visual_coverage_ok_false", "boundary",
            "low_verification_confidence", "issues_reported", "pages_truncated"]


def test_comparison_reports_risk_precision_and_mismatch_recall():
    scores = {"A": 2, "B": 3, "C": 1, "D": 2}
    reference = {"A": 2, "B": 2, "C": 2, "D": 2}
    risks = {"A": [], "B": ["boundary"], "C": [], "D": ["issues_reported"]}
    result = _comparison(scores, reference, risks)
    assert result["risk_precision"] == .5
    assert result["mismatch_recall"] == .5
    assert result["nonrisk"]["exact_rate"] == .5
    assert result["exact_by_predicted_score"] == {
        "1": {"n": 1, "exact": 0, "exact_rate": 0.0},
        "2": {"n": 2, "exact": 2, "exact_rate": 1.0},
        "3": {"n": 1, "exact": 0, "exact_rate": 0.0},
    }
