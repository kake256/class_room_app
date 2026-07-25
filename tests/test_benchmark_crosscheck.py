import asyncio

from grader.benchmark_crosscheck import (
    CrosscheckBatchGrader, _human_internal_score, _resolve, _risk_reasons,
)
from grader.config import Config
from grader.course_data import CoursePaths


def _grade(score, confidence=.9, evidence="答案の根拠"):
    return {
        "item_id": "A0001", "internal_score": score, "confidence": confidence,
        "reason": "基準に基づく判断", "evidence": evidence,
    }


def test_risk_reasons_select_only_disagreement_or_material_risk():
    text = {"status": "ready", "available_pages": 1, "total_pages": 1}
    assert _risk_reasons(
        text, _grade(2), _grade(2), max_visual_pages=6,
        confidence_threshold=.6) == []
    assert _risk_reasons(
        text, _grade(2), _grade(3, confidence=.5, evidence=""),
        max_visual_pages=6, confidence_threshold=.6) == [
            "model_disagreement", "low_confidence", "weak_evidence"]


def test_risk_reasons_cover_missing_and_page_truncation():
    visual = {"status": "visual_required", "available_pages": 8, "total_pages": 8}
    assert _risk_reasons(
        visual, None, _grade(2), max_visual_pages=6,
        confidence_threshold=.6) == ["primary_missing", "pages_truncated"]


def test_resolution_uses_agreement_and_adjudicator_without_forcing_third_score():
    assert _resolve(_grade(2), _grade(2), None, risky=False) == (
        2, "initial_agreement")
    assert _resolve(_grade(2), _grade(3), _grade(3), risky=True) == (
        3, "adjudicator_selected_initial")
    assert _resolve(_grade(1), _grade(2), _grade(3), risky=True) == (
        None, "unresolved_review")
    assert _resolve(_grade(2), _grade(2), _grade(1), risky=True) == (
        None, "risk_disagreement_review")
    assert _resolve(_grade(2), None, _grade(2), risky=True) == (
        2, "adjudicator_selected_initial")


def test_human_score_is_reversed_through_confirmed_mapping():
    settings = {"score_mapping": {"0": 0, "1": 8, "2": 9, "3": 10}}
    assert [_human_internal_score(settings, value) for value in (0, 8, 9, 10)] == [0, 1, 2, 3]
    assert _human_internal_score(None, 2.4) == 2


def test_visual_batch_keeps_answer_boundaries_and_prior_keys(tmp_path, monkeypatch):
    cfg = Config({
        "paths": {"data_dir": str(tmp_path)},
        "vllm": {"base_url": "http://invalid/v1"},
    })
    grader = CrosscheckBatchGrader(cfg, "456", {
        "confirmed": True, "notes": "rubric", "levels": {}, "score_mapping": {},
    }, "model")
    paths = CoursePaths(cfg, "123", "456")
    items = [
        {"item_id": "A1", "available_pages": 1, "row": {"student_id": "s1"}},
        {"item_id": "A2", "available_pages": 1, "row": {"student_id": "s2"}},
    ]
    prior = {
        key: {"primary": _grade(1), "secondary": _grade(2)} for key in ("A1", "A2")
    }
    calls = []

    def fake_page(_paths, row, page_number):
        return {
            "status": "ready", "text": row["student_id"],
            "image": {"data_base64": f"image-{row['student_id']}-{page_number}"},
        }

    async def fake_complete(keys, content, *, prior=None):
        calls.append((keys, content, prior))
        return {key: _grade(2) for key in keys}

    monkeypatch.setattr("grader.benchmark_crosscheck.submission_page", fake_page)
    monkeypatch.setattr(grader, "_complete", fake_complete)
    result = asyncio.run(grader.grade_visual_batch(items, paths, prior=prior))
    assert set(result) == {"A1", "A2"}
    keys, content, received_prior = calls[0]
    joined = "\n".join(str(block.get("text") or "") for block in content)
    assert keys == ["A1", "A2"] and received_prior is prior
    assert "<<<ANSWER A1>>>" in joined and "<<<END ANSWER A1>>>" in joined
    assert "<<<ANSWER A2>>>" in joined and "<<<END ANSWER A2>>>" in joined


def test_visual_batch_recursively_splits_after_batch_failure(tmp_path, monkeypatch):
    cfg = Config({"paths": {"data_dir": str(tmp_path)},
                  "vllm": {"base_url": "http://invalid/v1"}})
    grader = CrosscheckBatchGrader(cfg, "456", {
        "confirmed": True, "notes": "rubric", "levels": {}, "score_mapping": {},
    }, "model")
    paths = CoursePaths(cfg, "123", "456")
    items = [
        {"item_id": key, "available_pages": 1, "row": {"student_id": key}}
        for key in ("A1", "A2", "A3")
    ]
    batch_sizes = []

    monkeypatch.setattr("grader.benchmark_crosscheck.submission_page", lambda *_args: {
        "status": "ready", "text": "text", "image": {"data_base64": "image"}})

    async def fake_complete(keys, _content, *, prior=None):
        batch_sizes.append(len(keys))
        if len(keys) > 1:
            raise ValueError("batch too large")
        return {keys[0]: _grade(2)}

    monkeypatch.setattr(grader, "_complete", fake_complete)
    result = asyncio.run(grader.grade_visual_batch(items, paths))
    assert set(result) == {"A1", "A2", "A3"}
    assert batch_sizes[0] == 3 and batch_sizes.count(1) == 3
