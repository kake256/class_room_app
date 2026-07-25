from grader.benchmark_primary_risk import (
    _comparison_metrics, _group_metrics, _primary_risk_reasons,
)


def _grade(score=2, confidence=.9, evidence="答案の根拠"):
    return {"internal_score": score, "confidence": confidence, "evidence": evidence}


def test_primary_risk_ignores_visual_status_and_three_without_material_reason():
    item = {"status": "visual_required", "available_pages": 3, "total_pages": 3}
    assert _primary_risk_reasons(
        item, _grade(3), max_visual_pages=8, confidence_threshold=.6) == []


def test_primary_risk_covers_configured_material_reasons():
    item = {"status": "visual_required", "available_pages": 9, "total_pages": 9}
    assert _primary_risk_reasons(
        item, _grade(0, confidence=.6, evidence="短"),
        max_visual_pages=8, confidence_threshold=.6) == [
            "low_confidence", "weak_evidence", "pages_truncated", "zero_score"]
    assert _primary_risk_reasons(
        item, None, max_visual_pages=8,
        confidence_threshold=.6) == ["primary_missing"]


def test_group_metrics_reports_nonrisk_accuracy_and_large_differences():
    predicted = {"A": 2, "B": 3, "C": 0, "D": 3}
    reference = {"A": 2, "B": 2, "C": 3, "D": 3}
    risks = {"A": [], "B": [], "C": ["zero_score"], "D": []}
    result = _group_metrics(predicted, reference, risks)
    assert result["overall"]["n"] == 4
    assert result["overall"]["two_or_more_difference_count"] == 1
    assert result["risk"]["exact_rate"] == 0
    assert result["nonrisk"]["exact_rate"] == .6667
    assert _comparison_metrics({}, reference) == {"n": 0}
