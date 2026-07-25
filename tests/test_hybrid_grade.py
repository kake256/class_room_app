import asyncio
import json

import pytest

from grader.config import Config
from grader.course_data import CoursePaths
from grader.course_settings import settings_fingerprint
from grader.external_grading import ExternalProposalStore
from grader.hybrid_grade import (
    HybridLocalGrader, _evidence_verification, _needs_model_review, _text_batches,
    _validation_reasons, grade_hybrid,
)


def _cfg(tmp_path):
    return Config({
        "paths": {"data_dir": str(tmp_path / "data")},
        "vllm": {"model": "local-test", "base_url": "http://invalid/v1"},
        "hybrid_grading": {
            "text_batch_max_items": 2, "text_batch_max_chars": 100,
            "concurrency": 2, "review_confidence_threshold": 0.6,
        },
    })


def _settings():
    return {
        "confirmed": True, "notes": "fixed rubric",
        "levels": {"0": "none", "1": "basic", "2": "good", "3": "excellent"},
        "score_mapping": {"0": 0, "1": 8, "2": 9, "3": 10},
        "late_penalty": 0, "max_points": 10,
    }


def test_text_batches_bound_items_and_characters():
    items = [
        {"answer_text": "a" * 40}, {"answer_text": "b" * 40},
        {"answer_text": "c" * 40}, {"answer_text": "d" * 120},
    ]
    batches = _text_batches(items, max_items=3, max_chars=100)
    assert [len(batch) for batch in batches] == [2, 1, 1]


def test_result_validation_rejects_missing_duplicate_and_invalid_scores():
    valid = {"results": [
        {"item_id": "A", "internal_score": 2, "confidence": .8,
         "reason": "ok", "evidence": "text"},
        {"item_id": "B", "internal_score": 1, "confidence": .7,
         "reason": "ok", "evidence": "text"},
    ]}
    assert len(HybridLocalGrader._validate(valid, ["A", "B"])) == 2
    with pytest.raises(ValueError):
        HybridLocalGrader._validate({"results": valid["results"][:1]}, ["A", "B"])
    with pytest.raises(ValueError):
        HybridLocalGrader._validate({"results": [valid["results"][0]] * 2}, ["A", "B"])
    bad = {**valid["results"][0], "internal_score": 4}
    with pytest.raises(ValueError):
        HybridLocalGrader._validate({"results": [bad]}, ["A"])


def test_hybrid_pipeline_batches_text_falls_back_to_visual_and_saves(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    owner, course, coursework = "a" * 64, "123", "456"
    paths = CoursePaths(cfg, course, coursework)
    paths.root.mkdir(parents=True)
    settings = _settings()
    paths.settings.write_text(json.dumps(settings), encoding="utf-8")
    rows = [
        {"student_id": "1", "state": "TURNED_IN"},
        {"student_id": "2", "state": "TURNED_IN"},
        {"student_id": "3", "state": "TURNED_IN"},
    ]
    paths.meta.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")

    def fake_text(_paths, row):
        if row["student_id"] == "3":
            return {"status": "visual_required", "available_pages": 1, "text_chars": 200}
        return {"status": "ready", "answer_text": "answer " * 10}

    calls = {"text": [], "visual": 0, "reviews": 0}

    async def fake_batch(self, items, *, review=False):
        calls["text"].append(len(items))
        if review:
            calls["reviews"] += 1
        return [{
            "item_id": item["item_id"], "internal_score": 2,
            "confidence": .9 if review or item["item_id"] != "A0001" else .5,
            "reason": "rubric match", "evidence": "answer answer",
        } for item in items]

    async def fake_visual(self, item, _paths, *, review=False):
        calls["visual"] += 1
        return {"item_id": item["item_id"], "internal_score": 3,
                "confidence": .8, "reason": "visual match",
                "evidence": "see attached figure explanation"}

    monkeypatch.setattr("grader.hybrid_grade.submission_text", fake_text)
    monkeypatch.setattr(HybridLocalGrader, "grade_text_batch", fake_batch)
    monkeypatch.setattr(HybridLocalGrader, "grade_visual", fake_visual)
    result = asyncio.run(grade_hybrid(cfg, course, coursework, owner))
    assert result["status"] == "succeeded"
    assert result["text_count"] == 2 and result["visual_count"] == 1
    assert result["text_batch_count"] == 2  # 100文字上限により1件ずつ
    # 低確信のA0001だけがQwen3へ回る(A0003は画像答案だが視覚依存の申告がない)。
    assert result["reviewed_low_confidence"] == 1
    assert result["distribution"] == {"2": 2, "3": 1}
    assert calls["visual"] == 1 and calls["reviews"] == 1

    store = ExternalProposalStore(cfg)
    current = store.current(owner, course, coursework, settings_fingerprint(settings))
    assert len(current) == 3
    repeated = asyncio.run(grade_hybrid(cfg, course, coursework, owner))
    assert repeated["pending"] == 0 and repeated["skipped_existing"] == 3


def test_hybrid_pipeline_saves_each_batch_before_the_run_finishes(tmp_path, monkeypatch):
    """逐次保存: 全バッチの完了を待たず、完了済みバッチから随時ストアへ反映される。"""
    cfg = _cfg(tmp_path)
    owner, course, coursework = "a" * 64, "123", "456"
    paths = CoursePaths(cfg, course, coursework)
    paths.root.mkdir(parents=True)
    settings = _settings()
    paths.settings.write_text(json.dumps(settings), encoding="utf-8")
    rows = [{"student_id": "1", "state": "TURNED_IN"}, {"student_id": "2", "state": "TURNED_IN"}]
    paths.meta.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")

    def fake_text(_paths, row):
        return {"status": "ready", "answer_text": "answer " * 10}

    seen_before_second_batch_finished = {}

    async def fake_batch(self, items, *, review=False):
        item_id = items[0]["item_id"]
        if item_id == "A0002":
            # 先に完了したはずのA0001バッチが、このバッチの完了を待たず
            # 既にストアへ保存されていることを確認する。
            await asyncio.sleep(0.05)
            store = ExternalProposalStore(cfg)
            seen_before_second_batch_finished["current"] = store.current(
                owner, course, coursework, settings_fingerprint(settings))
        return [{
            "item_id": item_id, "internal_score": 2, "confidence": .9,
            "reason": "rubric match", "evidence": "answer answer",
        } for item in items]

    monkeypatch.setattr("grader.hybrid_grade.submission_text", fake_text)
    monkeypatch.setattr(HybridLocalGrader, "grade_text_batch", fake_batch)
    result = asyncio.run(grade_hybrid(cfg, course, coursework, owner))
    assert result["status"] == "succeeded"
    assert len(seen_before_second_batch_finished["current"]) == 1


def test_review_state_transition_is_saved_even_when_score_and_reason_are_unchanged(tmp_path, monkeypatch):
    """model_review_pending→ready_for_human_reviewの遷移は、reviewの内容が
    一次結果と同一でも(save_batchの内容比較でスキップされず)必ず保存される。"""
    cfg = _cfg(tmp_path)
    owner, course, coursework = "a" * 64, "123", "456"
    paths = CoursePaths(cfg, course, coursework)
    paths.root.mkdir(parents=True)
    settings = _settings()
    paths.settings.write_text(json.dumps(settings), encoding="utf-8")
    rows = [{"student_id": "1", "state": "TURNED_IN"}]
    paths.meta.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")

    def fake_text(_paths, row):
        return {"status": "ready", "answer_text": "answer " * 10}

    async def fake_batch(self, items, *, review=False):
        # review=True でも一次結果と全く同じ内容(点数・理由・根拠・確信度)を返す。
        return [{
            "item_id": item["item_id"], "internal_score": 1, "confidence": .5,
            "reason": "same reason", "evidence": "same evidence",
        } for item in items]

    monkeypatch.setattr("grader.hybrid_grade.submission_text", fake_text)
    monkeypatch.setattr(HybridLocalGrader, "grade_text_batch", fake_batch)
    result = asyncio.run(grade_hybrid(cfg, course, coursework, owner))
    assert result["status"] == "succeeded" and result["reviewed_low_confidence"] == 1

    store = ExternalProposalStore(cfg)
    fingerprint = settings_fingerprint(settings)
    ref = store.submission_ref(owner, course, coursework, "1")
    raw = store.load(owner, course, coursework)[ref]
    # レビューしても低確信が解消しなかったため未解決。model_review_pendingから
    # 遷移していること自体が、内容同一でも保存された証拠になる。
    assert raw["state"] == "model_review_unresolved"
    assert raw["remaining_validation_reasons"] == ["low_confidence", "evidence_not_verified"]
    current = store.current(owner, course, coursework, fingerprint)
    assert current[ref]["state"] == "model_review_unresolved"


def test_one_failed_batch_does_not_block_or_lose_other_successful_batches(tmp_path, monkeypatch):
    """asyncio.as_completedで1バッチが例外になっても、他の成功済みバッチの
    保存結果は失われず、失敗分はmark_failed経由でstate=failedとして記録される。"""
    cfg = _cfg(tmp_path)
    owner, course, coursework = "a" * 64, "123", "456"
    paths = CoursePaths(cfg, course, coursework)
    paths.root.mkdir(parents=True)
    settings = _settings()
    paths.settings.write_text(json.dumps(settings), encoding="utf-8")
    rows = [{"student_id": "1", "state": "TURNED_IN"}, {"student_id": "2", "state": "TURNED_IN"}]
    paths.meta.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")

    def fake_text(_paths, row):
        return {"status": "ready", "answer_text": "answer " * 10}

    async def fake_batch(self, items, *, review=False):
        item_id = items[0]["item_id"]
        if item_id == "A0002":
            raise RuntimeError("simulated batch failure")
        return [{
            "item_id": item_id, "internal_score": 2, "confidence": .9,
            "reason": "rubric match", "evidence": "answer answer",
        } for item in items]

    monkeypatch.setattr("grader.hybrid_grade.submission_text", fake_text)
    monkeypatch.setattr(HybridLocalGrader, "grade_text_batch", fake_batch)
    result = asyncio.run(grade_hybrid(cfg, course, coursework, owner))
    assert result["error_count"] == 1

    store = ExternalProposalStore(cfg)
    fingerprint = settings_fingerprint(settings)
    ref_ok = store.submission_ref(owner, course, coursework, "1")
    ref_failed = store.submission_ref(owner, course, coursework, "2")
    current = store.current(owner, course, coursework, fingerprint)
    assert ref_ok in current and current[ref_ok]["state"] == "ready_for_human_review"
    assert ref_failed not in current  # failedはcurrent()のstatus=="ok"判定に含まれない
    raw = store.load(owner, course, coursework)
    assert raw[ref_failed]["status"] == "failed" and raw[ref_failed]["state"] == "failed"


READY_ITEM = {"status": "ready", "answer_text": "this is the full answer text used for matching"}
VISUAL_ITEM = {"status": "visual_required", "available_pages": 2, "text_chars": 200}


def _result(**overrides):
    base = {
        "internal_score": 2, "rubric_level": "2", "rubric_level_raw": "2", "boundary": False,
        "visual_dependency": False, "visual_confirmed": False,
        "confidence": 0.9, "reason": "ok", "evidence": "the full answer text",
    }
    base.update(overrides)
    return base


def test_signals_that_must_not_trigger_review_on_their_own():
    boundary_only = _validation_reasons(READY_ITEM, _result(boundary=True), confidence_threshold=0.6)
    assert boundary_only == ["boundary"]
    assert not _needs_model_review(boundary_only)

    # 画像素材があるだけ(視覚依存の判断ではない)ならQwen3へ送らない。
    visual_material_only = _validation_reasons(
        VISUAL_ITEM, _result(evidence="a sufficiently long evidence string"),
        confidence_threshold=0.6)
    assert visual_material_only == ["evidence_not_verified_visual", "has_visual_material"]
    # 画像答案の照合未実施は記録するが、それだけでQwen3へ送ると画像答案が
    # 全件対象になる(実測67.7%)ため単独では送らない。
    assert not _needs_model_review(visual_material_only)

    # rubric_levelの表記不一致は正規化で解決するため単独では送らない。
    mismatch = _validation_reasons(
        READY_ITEM, _result(rubric_level_raw="0"), confidence_threshold=0.6)
    assert mismatch == ["score_rubric_mismatch"]
    assert not _needs_model_review(mismatch)

    # バッチ二分再試行は正常な復旧経路なので単独では送らない。
    retried = _validation_reasons(
        READY_ITEM, _result(batch_retry_used=True), confidence_threshold=0.6)
    assert retried == ["batch_retry_used"]
    assert not _needs_model_review(retried)


def test_visual_review_required_only_when_dependent_and_unconfirmed():
    # 視覚依存だが確認できていない → Qwen3へ送る
    unconfirmed = _validation_reasons(
        VISUAL_ITEM, _result(visual_dependency=True, visual_confirmed=False),
        confidence_threshold=0.6)
    assert "visual_review_required" in unconfirmed and _needs_model_review(unconfirmed)

    # 視覚依存かつ確認済み → visual_review_requiredは立たない
    confirmed = _validation_reasons(
        VISUAL_ITEM, _result(visual_dependency=True, visual_confirmed=True),
        confidence_threshold=0.6)
    assert "visual_review_required" not in confirmed

    # ページが打ち切られている場合は確認済みでも要確認
    truncated_item = {**VISUAL_ITEM, "total_pages": 9, "available_pages": 2}
    truncated = _validation_reasons(
        truncated_item, _result(visual_dependency=True, visual_confirmed=True),
        confidence_threshold=0.6)
    assert "visual_review_required" in truncated


def test_evidence_verification_status_distinguishes_visual_from_verified():
    assert _evidence_verification(READY_ITEM, _result()) == "verified"
    assert _evidence_verification(
        READY_ITEM, _result(evidence="totally unrelated fabricated quote")) == "not_found"
    assert _evidence_verification(READY_ITEM, _result(evidence="no")) == "too_short"
    # 画像答案は照合未実施。成功扱いにしない。
    assert _evidence_verification(
        VISUAL_ITEM, _result(evidence="a sufficiently long evidence string")) == "not_run_visual"


def test_validation_reasons_detects_low_confidence_short_and_unverified_evidence():
    low_conf = _validation_reasons(READY_ITEM, _result(confidence=0.4), confidence_threshold=0.6)
    assert "low_confidence" in low_conf and _needs_model_review(low_conf)

    too_short = _validation_reasons(READY_ITEM, _result(evidence="none"), confidence_threshold=0.6)
    assert "evidence_too_short" in too_short and _needs_model_review(too_short)

    not_verified = _validation_reasons(
        READY_ITEM, _result(evidence="totally unrelated fabricated quote here"),
        confidence_threshold=0.6)
    assert "evidence_not_verified" in not_verified and _needs_model_review(not_verified)

    parse_recovered = _validation_reasons(
        READY_ITEM, _result(item_parse_recovered=True), confidence_threshold=0.6)
    assert "item_parse_recovered" in parse_recovered and _needs_model_review(parse_recovered)


def test_review_reasons_ocr_quality_low_for_sparse_visual_extraction():
    sparse_visual = {"status": "visual_required", "available_pages": 2, "text_chars": 10}
    reasons = _validation_reasons(sparse_visual, _result(evidence="ok"), confidence_threshold=0.6)
    assert "ocr_quality_low" in reasons and _needs_model_review(reasons)


def test_review_failure_keeps_primary_score_and_marks_model_review_failed(tmp_path, monkeypatch):
    """Qwen3レビュー自体が失敗した場合、一次(Qwen2.5)結果は保持し、
    state="model_review_failed"として自動下書き対象から除外する。"""
    cfg = _cfg(tmp_path)
    owner, course, coursework = "a" * 64, "123", "456"
    paths = CoursePaths(cfg, course, coursework)
    paths.root.mkdir(parents=True)
    settings = _settings()
    paths.settings.write_text(json.dumps(settings), encoding="utf-8")
    rows = [{"student_id": "1", "state": "TURNED_IN"}]
    paths.meta.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")

    def fake_text(_paths, row):
        return {"status": "ready", "answer_text": "this is the full answer text used for matching"}

    async def fake_batch(self, items, *, review=False):
        if review:
            raise RuntimeError("simulated review failure")
        return [{
            "item_id": items[0]["item_id"], "internal_score": 1, "confidence": .3,
            "reason": "primary reason", "evidence": "this is the full answer text",
        }]

    monkeypatch.setattr("grader.hybrid_grade.submission_text", fake_text)
    monkeypatch.setattr(HybridLocalGrader, "grade_text_batch", fake_batch)
    result = asyncio.run(grade_hybrid(cfg, course, coursework, owner))
    assert result["status"] == "succeeded"  # レビュー失敗はerror_countに含めない(一次結果は成功済み)

    store = ExternalProposalStore(cfg)
    ref = store.submission_ref(owner, course, coursework, "1")
    raw = store.load(owner, course, coursework)[ref]
    assert raw["state"] == "model_review_failed"
    assert raw["internal_score"] == 1  # 一次結果のスコアを保持
    assert raw["primary_result"]["internal_score"] == 1
    assert raw["review_result"] is None
    fingerprint = settings_fingerprint(settings)
    current = store.current(owner, course, coursework, fingerprint)
    assert ref in current and current[ref]["state"] == "model_review_failed"


def test_review_success_stores_primary_review_and_changed_score_separately(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    owner, course, coursework = "a" * 64, "123", "456"
    paths = CoursePaths(cfg, course, coursework)
    paths.root.mkdir(parents=True)
    settings = _settings()
    paths.settings.write_text(json.dumps(settings), encoding="utf-8")
    rows = [{"student_id": "1", "state": "TURNED_IN"}]
    paths.meta.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")

    def fake_text(_paths, row):
        return {"status": "ready", "answer_text": "this is the full answer text used for matching"}

    async def fake_batch(self, items, *, review=False):
        if review:
            return [{
                "item_id": items[0]["item_id"], "internal_score": 2, "confidence": .95,
                "reason": "review reason", "evidence": "this is the full answer text",
            }]
        return [{
            "item_id": items[0]["item_id"], "internal_score": 1, "confidence": .3,
            "reason": "primary reason", "evidence": "this is the full answer text",
        }]

    monkeypatch.setattr("grader.hybrid_grade.submission_text", fake_text)
    monkeypatch.setattr(HybridLocalGrader, "grade_text_batch", fake_batch)
    result = asyncio.run(grade_hybrid(cfg, course, coursework, owner))
    assert result["status"] == "succeeded"
    assert set(result["timestamps"]) >= {
        "first_primary_saved_at", "primary_completed_at",
        "review_started_at", "first_ready_for_human_review_at", "review_completed_at",
    }

    store = ExternalProposalStore(cfg)
    ref = store.submission_ref(owner, course, coursework, "1")
    raw = store.load(owner, course, coursework)[ref]
    assert raw["state"] == "ready_for_human_review"
    assert raw["primary_result"]["internal_score"] == 1
    assert raw["review_result"]["internal_score"] == 2
    assert raw["review_changed_score"] is True
    # review_wins: レビュー結果(2点)を提案値として採用する。
    assert raw["internal_score"] == 2
    assert raw["model_disagreement"] is True and raw["score_delta"] == 1
    assert raw["remaining_validation_reasons"] == []



def test_request_timing_summary_reports_p50_p95_and_first_ready():
    """リクエスト単位計測をp50/p95・初回ready秒へ要約する(生ログは残さない)。"""
    from grader.hybrid_grade import _timing_summary

    assert _timing_summary([]) == {"requests": 0}
    timings = [
        {"items": 1, "review": False, "visual": True,
         "started_at": 100.0, "completed_at": 110.0, "latency_seconds": 10.0},
        {"items": 3, "review": False, "visual": False,
         "started_at": 100.0, "completed_at": 102.0, "latency_seconds": 2.0},
        {"items": 1, "review": True, "visual": True,
         "started_at": 112.0, "completed_at": 152.0, "latency_seconds": 40.0},
    ]
    summary = _timing_summary(timings)
    assert summary["requests"] == 3
    assert summary["first_ready_seconds"] == 2.0     # 最初に完了したのはテキスト(102.0-100.0)
    assert summary["all"]["latency_p50"] == 10.0
    assert summary["all"]["latency_p95"] == 40.0
    assert summary["text"]["requests"] == 1 and summary["visual"]["requests"] == 2
    assert summary["review"]["requests"] == 1
    # 答案本文・プロンプトを含まない(件数・時刻・秒数のみ)
    flat = json.dumps(summary, ensure_ascii=False)
    assert "answer" not in flat and "prompt" not in flat


def test_teacher_review_duration_rejects_impossible_values(tmp_path):
    """確認所要時間は異常値を採用しない。"""
    from grader.config import Config as _Config
    from grader.teacher_review import TeacherReviewStore

    store = TeacherReviewStore(_Config({"paths": {"data_dir": str(tmp_path)}}), clock=lambda: 1000)
    ok = store.save("owner", "200", "300", "400", score=2.0, confirmed=True,
                    review_started_at=940)
    assert ok["review_duration_seconds"] == 60.0
    assert ok["review_started_at"] == 940 and ok["review_completed_at"] == 1000
    future = store.save("owner", "200", "300", "401", score=2.0, confirmed=True,
                        review_started_at=2000)
    assert "review_duration_seconds" not in future
    stale = store.save("owner", "200", "300", "402", score=2.0, confirmed=True,
                       review_started_at=1000 - 9 * 3600)
    assert "review_duration_seconds" not in stale
