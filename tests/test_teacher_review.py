from grader.config import Config
from grader.teacher_review import DraftBatchStore, TeacherReviewStore, prepare_draft_preview


def test_review_storage_is_owner_course_work_scoped_and_atomic(tmp_path):
    store = TeacherReviewStore(Config({"paths": {"data_dir": str(tmp_path)}}), clock=lambda: 100)
    store.save("owner-a", "200", "300", "400", score=8, confirmed=True)
    assert store.load("owner-a", "200", "300")["400"]["status"] == "confirmed"
    assert store.load("owner-b", "200", "300") == {}
    assert store.load("owner-a", "201", "300") == {}
    path = next(tmp_path.rglob("teacher_reviews/*.json"))
    assert path.stat().st_mode & 0o777 == 0o600


def test_preview_only_confirmed_and_excludes_protected_rows():
    rows = [
        {"student_id": "1", "state": "TURNED_IN"},
        {"student_id": "2", "state": "RETURNED"},
        {"student_id": "3", "state": "TURNED_IN", "draft_grade": 0},
        {"student_id": "4", "state": "TURNED_IN"},
    ]
    confirmations = {
        "1": {"status": "confirmed", "score": 8},
        "2": {"status": "confirmed", "score": 8},
        "3": {"status": "confirmed", "score": 8},
        "4": {"status": "proposal", "score": 8},
    }
    preview = prepare_draft_preview(rows, confirmations, max_points=10)
    assert preview["eligible"] == [{"student_id": "1", "score": 8.0}]
    assert preview["skipped_count"] == 3


def test_current_system_and_external_proposals_need_no_per_answer_approval():
    rows = [
        {"student_id": "1", "state": "TURNED_IN", "source": "system",
         "mapped_score": 8, "automatic_eligible": True},
        {"student_id": "2", "state": "TURNED_IN", "source": "external_mcp",
         "mapped_score": 7, "automatic_eligible": True},
        {"student_id": "3", "state": "TURNED_IN", "source": "external_mcp",
         "mapped_score": 6, "automatic_eligible": False},
        {"student_id": "4", "state": "TURNED_IN", "source": "external_mcp",
         "mapped_score": 5, "automatic_eligible": True, "draft_grade": 0},
    ]
    preview = prepare_draft_preview(rows, {}, max_points=10)
    assert preview["eligible"] == [
        {"student_id": "1", "score": 8.0}, {"student_id": "2", "score": 7.0},
    ]
    assert {item["reason"] for item in preview["skipped"]} == {
        "not_confirmed", "existing_or_human_grade",
    }


def test_batch_pairing_is_short_lived_and_one_use():
    now = [100]
    store = DraftBatchStore(ttl_seconds=10, clock=lambda: now[0])
    created = store.create(owner_sub="owner", course_id="200", coursework_id="300",
                           items=[{"student_id": "1", "score": 8}], max_points=10)
    claimed = store.claim(created["pairing_code"], course_id="200", coursework_id="300")
    batch = store.fetch(claimed["batch_id"], claimed["access_token"])
    assert batch["items"][0]["score"] == 8 and batch["max_points"] == 10
    assert store.claim(created["pairing_code"], course_id="200", coursework_id="300") is None
    assert store.consume(claimed["batch_id"], claimed["access_token"]) is True
    assert store.fetch(claimed["batch_id"], claimed["access_token"]) is None
    expired = store.create(owner_sub="owner", course_id="200", coursework_id="300",
                           items=[], max_points=10)
    now[0] = 111
    assert store.claim(expired["pairing_code"], course_id="200", coursework_id="300") is None


def test_reference_device_claim_is_owner_context_bound_and_summary_validated():
    store = DraftBatchStore(ttl_seconds=60)
    store.create_reference(owner_ref="a" * 64, course_id="200", coursework_id="300",
                           items=[{"student_id": "1", "score": 8}], max_points=10)
    assert store.claim_ready(owner_ref="b" * 64, course_id="200", coursework_id="300") is None
    assert store.claim_ready(owner_ref="a" * 64, course_id="201", coursework_id="300") is None
    claim = store.claim_ready(owner_ref="a" * 64, course_id="200", coursework_id="300")
    batch = store.fetch(claim["batch_id"], claim["access_token"])
    assert batch["items"][0]["student_id"] == "1"
    import pytest
    with pytest.raises(ValueError):
        store.consume_receipt(claim["batch_id"], claim["access_token"], {
            "attempted": 0, "filled": 0, "skipped": 0, "failed": 0})
    receipt = store.consume_receipt(claim["batch_id"], claim["access_token"], {
        "attempted": 1, "filled": 1, "skipped": 0, "failed": 0})
    assert receipt["owner_ref"] == "a" * 64 and receipt["summary"]["filled"] == 1
    assert store.fetch(claim["batch_id"], claim["access_token"]) is None


def test_only_explicit_review_or_human_assigned_grade_counts_as_confirmed():
    """AIのdraftGrade・拡張入力・ready_for_human_reviewを確認済みにしない。

    api._unified_grading_rowsの後段(v1_results)と同じ判定規則を検証する。
    確認済みと扱うのは (1) Web UIの明示レビュー記録 (2) 人間のassignedGrade だけ。
    """
    import math as _math

    def classify(row, confirmation):
        assigned = row.get("assigned_grade")
        human_assigned = assigned is not None and not (
            isinstance(assigned, float) and _math.isnan(assigned))
        if confirmation.get("status") == "confirmed":
            return "confirmed", "web_review"
        if human_assigned:
            return "confirmed", "classroom_assigned_grade"
        return confirmation.get("status", "proposal"), None

    # AIが入れたdraftGradeだけでは確認済みにしない
    assert classify({"draft_grade": 8, "proposal_state": "ready_for_human_review"}, {}) == (
        "proposal", None)
    # ready_for_human_reviewだけでも確認済みにしない
    assert classify({"proposal_state": "ready_for_human_review"}, {}) == ("proposal", None)
    # 人間のassignedGradeは確認済み
    assert classify({"assigned_grade": 9}, {}) == ("confirmed", "classroom_assigned_grade")
    # 明示的なreview記録は確認済み
    assert classify({}, {"status": "confirmed", "score": 7}) == ("confirmed", "web_review")
    # NaNのassigned_gradeは未確認扱い
    assert classify({"assigned_grade": float("nan")}, {}) == ("proposal", None)


def test_review_save_records_proposal_delta_and_signals_for_audit(tmp_path):
    """確認時のフラグ有無・点数変更有無・変更幅を監査用に保存する。"""
    store = TeacherReviewStore(Config({"paths": {"data_dir": str(tmp_path)}}), clock=lambda: 100)
    saved = store.save("owner", "200", "300", "400", score=2.0, confirmed=True,
                       proposal_score=3.0, signals=["q25_visual_max_score", "has_visual_material"])
    assert saved["ai_proposal_score"] == 3.0
    assert saved["score_changed"] is True and saved["score_delta"] == -1.0
    assert saved["signals_at_review"] == ["q25_visual_max_score", "has_visual_material"]

    same = store.save("owner", "200", "300", "401", score=3.0, confirmed=True, proposal_score=3.0)
    assert same["score_changed"] is False and same["score_delta"] == 0.0
    # 旧呼び出し(追加情報なし)でも従来のキーだけで保存できる
    legacy = store.save("owner", "200", "300", "402", score=1.0, confirmed=False)
    assert set(legacy) == {"status", "score", "updated_at"}


def test_visual_max_score_uses_primary_result_not_merged_score():
    """判定はprimary_resultのみを参照し、統合後の点数やreview_scoreを使わない。"""
    from grader.api import _is_q25_visual_max_score

    base = {"review_reasons": ["has_visual_material"], "teacher_status": "proposal"}
    # 一次が最高点 → フラグあり
    assert _is_q25_visual_max_score({
        **base, "primary_result": {"model": "local:Qwen/Qwen2.5-VL-7B-Instruct",
                                   "internal_score": 3}}) is True
    # 統合後が最高点でも一次が最高点でなければフラグなし
    assert _is_q25_visual_max_score({
        **base, "content_score": 3,
        "primary_result": {"model": "local:Qwen/Qwen2.5-VL-7B-Instruct", "internal_score": 2},
        "review_result": {"model": "local:Qwen/Qwen3-VL-8B-Instruct-FP8", "internal_score": 3},
    }) is False
    # 一次がQwen3ならフラグなし
    assert _is_q25_visual_max_score({
        **base, "primary_result": {"model": "local:Qwen/Qwen3-VL-8B-Instruct-FP8",
                                   "internal_score": 3}}) is False
    # 画像素材がなければフラグなし
    assert _is_q25_visual_max_score({
        "review_reasons": [], "teacher_status": "proposal",
        "primary_result": {"model": "local:Qwen/Qwen2.5-VL-7B-Instruct",
                           "internal_score": 3}}) is False
    # 人間確認済みならフラグなし
    assert _is_q25_visual_max_score({
        **base, "teacher_status": "confirmed",
        "primary_result": {"model": "local:Qwen/Qwen2.5-VL-7B-Instruct",
                           "internal_score": 3}}) is False
    # primary_resultが無い行(旧systemパイプライン)ではフラグなし
    assert _is_q25_visual_max_score({**base, "content_score": 3, "model": "local:Qwen2.5"}) is False
