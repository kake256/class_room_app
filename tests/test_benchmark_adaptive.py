from grader.benchmark_adaptive import (
    _grade_has_weak_evidence, _resolve_scores, _risk_reasons,
)


def _grade(score, *, evidence="根拠", flags=None):
    return {
        "gate": {"pass": True, "reason": "ok"},
        "total": score,
        "flags": flags or [],
        "criteria": [
            {"name": name, "score": 1 if index < score else 0, "evidence": evidence}
            for index, name in enumerate(("quantitative", "method", "discussion"))
        ],
    }


def test_risk_reasons_cover_disagreement_flags_evidence_and_truncation():
    item = {"available_pages": 6, "total_pages": 8}
    reasons = _risk_reasons(
        item, [_grade(2, flags=["ambiguous"]), _grade(3, evidence="")])
    assert reasons == [
        "primary_disagreement", "flags", "weak_evidence", "pages_truncated"]


def test_risk_reasons_detect_missing_primary_run():
    assert _risk_reasons({"available_pages": 1, "total_pages": 1}, [_grade(2)]) == [
        "primary_missing"]
    assert _risk_reasons(
        {"available_pages": 8, "total_pages": 8}, [_grade(2), _grade(2)],
        max_visual_pages=6,
    ) == ["pages_truncated"]


def test_weak_evidence_validation():
    assert not _grade_has_weak_evidence(_grade(2))
    assert _grade_has_weak_evidence(_grade(2, evidence=""))
    assert _grade_has_weak_evidence({"criteria": []})


def test_score_resolution_requires_two_equal_votes():
    assert _resolve_scores([2, 2], None) == (2, "two_vote_agreement")
    assert _resolve_scores([2, 2], 1) == (2, "two_vote_agreement")
    assert _resolve_scores([2, 3], 3) == (3, "two_vote_agreement")
    assert _resolve_scores([1, 2], 3) == (None, "review_required")
    assert _resolve_scores([2], 2) == (2, "two_vote_agreement")
    assert _resolve_scores([], 2) == (None, "review_required")
