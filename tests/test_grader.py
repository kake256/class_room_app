"""ユニットテスト: render / 2回一致判定 / 判定区分 / 遅延減点(VLMはモック不要の純関数)。"""
import json

import fitz
import pytest

from grader.grade import clamp_total, consolidate
from grader.render import render_pdf
from grader.report import apply_late_policy, apply_length_bonus, classify
from grader.config import Config


def _run(scores, gate=True, flags=None):
    names = ["quantitative", "method", "discussion"]
    return {
        "gate": {"pass": gate, "reason": ""},
        "criteria": [
            {"name": n, "score": s, "evidence": "", "comment": ""}
            for n, s in zip(names, scores)
        ],
        "total": sum(scores),
        "flags": flags or [],
        "notable": None,
    }


# ---- render ----

def _make_pdf(path, n_pages):
    doc = fitz.open()
    for i in range(n_pages):
        page = doc.new_page()
        page.insert_text((72, 72), f"page {i + 1}")
    doc.save(path)
    doc.close()


def test_render_pages(tmp_path):
    pdf = tmp_path / "a.pdf"
    _make_pdf(pdf, 3)
    rr = render_pdf(pdf, tmp_path / "out", dpi=72, max_pages=8)
    assert len(rr.pages) == 3 and rr.total_pages == 3 and not rr.truncated
    assert all(p.exists() for p in rr.pages)


def test_render_truncation(tmp_path):
    pdf = tmp_path / "b.pdf"
    _make_pdf(pdf, 10)
    rr = render_pdf(pdf, tmp_path / "out", dpi=72, max_pages=8)
    assert len(rr.pages) == 8 and rr.total_pages == 10 and rr.truncated


def test_render_idempotent(tmp_path):
    pdf = tmp_path / "c.pdf"
    _make_pdf(pdf, 1)
    rr1 = render_pdf(pdf, tmp_path / "out", dpi=72)
    mtime = rr1.pages[0].stat().st_mtime_ns
    rr2 = render_pdf(pdf, tmp_path / "out", dpi=72)
    assert rr2.pages[0].stat().st_mtime_ns == mtime  # 再実行でも再レンダリングしない


# ---- clamp_total / consolidate ----

def test_clamp_total_gate_fail():
    assert clamp_total(_run([1, 1, 1], gate=False)) == 0


def test_clamp_total_rounding():
    assert clamp_total(_run([0.5, 1, 0])) == 2  # 1.5 → 四捨五入で2
    assert clamp_total(_run([0.5, 0, 0])) == 1  # 0.5 → 四捨五入で1(切り上げ)
    assert clamp_total(_run([1, 1, 0.5])) == 3  # 2.5 → 四捨五入で3(切り上げ)
    assert clamp_total(_run([1, 1, 1])) == 3


def test_consolidate_agree():
    c = consolidate([_run([1, 1, 0]), _run([1, 1, 0])])
    assert c["final_score"] == 2 and "inconsistent" not in c["flags"]


def test_consolidate_disagree_takes_min():
    c = consolidate([_run([1, 1, 1]), _run([1, 1, 0])])
    assert c["final_score"] == 2 and "inconsistent" in c["flags"]


def test_consolidate_gate_fail_flag():
    c = consolidate([_run([1, 1, 1], gate=False), _run([1, 1, 1])])
    assert c["final_score"] == 0 and "gate_failed" in c["flags"]


def test_consolidate_merges_llm_flags():
    c = consolidate([_run([1, 1, 0], flags=["blurry_figure"]), _run([1, 1, 0])])
    assert "blurry_figure" in c["flags"]


# ---- classify ----

def _result(score, flags=(), status="ok"):
    return {"status": status, "final_score": score, "flags": list(flags), "runs": []}


@pytest.mark.parametrize("score,expected", [(0, "auto_0"), (1, "auto_1"), (2, "auto_2")])
def test_classify_auto(score, expected):
    assert classify(_result(score)) == expected


def test_classify_candidate_3_never_auto():
    assert classify(_result(3)) == "candidate_3"


@pytest.mark.parametrize(
    "flags", [["inconsistent"], ["gate_failed"], ["format_violation"], ["pages_truncated"]]
)
def test_classify_review_flags(flags):
    assert classify(_result(2, flags)) == "review"


def test_classify_error_status():
    assert classify(_result(2, status="ERROR")) == "review"


def test_classify_late_flag_alone_not_review():
    assert classify(_result(2, ["late"])) == "auto_2"


def test_classify_unknown_flag_goes_review():
    # 判断に迷った旨のLLM flags もレビュー行き
    assert classify(_result(1, ["ambiguous_figure"])) == "review"


# ---- late policy ----

def test_late_penalty_applied():
    assert apply_late_policy(2, late=True) == (1, False)
    assert apply_late_policy(0, late=True) == (0, False)  # 0未満にしない


def test_late_not_late():
    assert apply_late_policy(2, late=False) == (2, False)


def test_late_waiver_for_full_score():
    assert apply_late_policy(3, late=True) == (3, True)  # 減点保留+waiver候補


# ---- normalize_gid ----

def test_normalize_gid():
    from grader.fetch import normalize_gid

    assert normalize_gid("MTIzNDU2Nzg5MDEy") == "123456789012"  # URLのBase64形式
    assert normalize_gid("123456789012") == "123456789012"      # 数値はそのまま
    assert normalize_gid(" NzUxOTQ0NDc1NDF6 ") != ""             # 前後空白も許容


# ---- pairwise ----

def test_decide_verdict():
    from grader.pairwise import decide_verdict

    assert decide_verdict([True, True]) == "keep_candidate"
    assert decide_verdict([True, False]) == "borderline"   # 見逃し防止: 降格しない
    assert decide_verdict([False, False]) == "demote"
    assert decide_verdict([]) == "error"


def test_classify_pairwise_verdicts():
    r = _result(3)
    r["pairwise"] = {"verdict": "keep_candidate", "runs": [1]}
    assert classify(r) == "candidate_3"
    r["pairwise"] = {"verdict": "borderline", "runs": [1]}
    assert classify(r) == "candidate_3"
    r["pairwise"] = {"verdict": "demote", "runs": [1]}
    assert classify(r) == "auto_2"
    r["pairwise"] = {"verdict": "error", "runs": []}
    assert classify(r) == "review"
    assert classify(_result(3)) == "candidate_3"  # 審判フェーズ未実施なら候補のまま


def test_classify_judge_hybrid():
    from grader.report import candidate_tier, judge_score

    # judge観点合計が閾値未満 → 降格
    r = _result(3)
    r["judge"] = {"status": "ok", "runs": [1], "final_score": 2, "crit_min_sum": 1.5}
    assert classify(r) == "auto_2"
    # 閾値以上なら候補維持
    r["judge"]["crit_min_sum"] = 2.5
    assert classify(r) == "candidate_3"
    # judgeが3点を付けた一次採点2点の答案は候補へ昇格(安全網)
    r2 = _result(2)
    r2["judge"] = {"status": "ok", "runs": [1], "final_score": 3, "crit_min_sum": 3.0}
    assert classify(r2) == "candidate_3"
    assert candidate_tier(r2) == "borderline"
    # tier: 両順勝ちはstrong
    r3 = _result(3)
    r3["pairwise"] = {"verdict": "keep_candidate", "runs": [1]}
    assert candidate_tier(r3) == "strong"
    assert candidate_tier(_result(3)) == "unrefined"
    # judge失敗時はjudge_scoreなし
    r4 = _result(2)
    r4["judge"] = {"status": "ERROR", "runs": []}
    assert judge_score(r4) is None
    assert classify(r4) == "auto_2"


# ---- lenient stance ----

def test_build_user_prompt_stance():
    from grader.rubric import ASSIGNMENT_SPECS, build_user_prompt

    rf_text, rf_key = ASSIGNMENT_SPECS["rf"]
    strict = build_user_prompt(rf_text, rf_key)
    assert "甘めに採点" not in strict and "低い方のスコア" in strict
    lenient = build_user_prompt(rf_text, rf_key, lenient=True)
    assert "甘めに採点" in lenient and "高い方のスコアを付ける" in lenient
    assert "甘めに採点" not in build_user_prompt(rf_text, rf_key, lenient=False)


def test_build_user_prompt_kansou():
    from grader.rubric import ASSIGNMENT_SPECS, build_user_prompt

    text, key = ASSIGNMENT_SPECS["kansou1"]
    p = build_user_prompt(text, key, lenient=True)
    assert "まとめの具体性" in p and "甘めに採点" in p
    assert "識別境界" not in p  # 実験用ルーブリックが混ざっていない
    t2, k2 = ASSIGNMENT_SPECS["tokubetsu0511"]
    p2 = build_user_prompt(t2, k2, lenient=True)
    assert "AIエージェント" in p2 and "まとめの具体性" in p2


def test_resolve_assignment():
    from grader.rubric import resolve_assignment

    # config の assignments マップで courseWorkId → キーを解決
    amap = {"111111111111": "sukina", "222222222222": "rf"}
    text, key = resolve_assignment("111111111111", amap)
    assert key == "EFFORT" and "好きな手法" in text
    assert resolve_assignment(None, amap)[1] == "EXPERIMENT"  # 既定はrf
    import pytest
    with pytest.raises(KeyError):
        resolve_assignment("999999999999", amap)      # 未登録IDはエラー
    with pytest.raises(KeyError):
        resolve_assignment("333333333333", {"333333333333": "bogus"})  # 未定義キー


@pytest.mark.parametrize("key,rubric", [
    ("ai_kansou", "KANSOU"), ("application_research", "RESEARCH"),
    ("distance", "DISTANCE"), ("knn", "KNN"),  # 2026-07-17 専用ルーブリック化に追従
    ("face_detection", "OBSERVATION"),
])
def test_historical_assignment_specs(key, rubric):
    from grader.rubric import ASSIGNMENT_SPECS, build_user_prompt
    text, actual = ASSIGNMENT_SPECS[key]
    assert actual == rubric
    prompt = build_user_prompt(text, actual, lenient=(actual == "KANSOU"))
    assert text in prompt and "ゲート条件" in prompt


def test_map_score_scales():
    from grader.push import AUTO_CATEGORIES, map_score

    assert map_score(2, 3) == 2                      # 3点満点はそのまま
    assert [map_score(s, 100) for s in range(4)] == [70, 75, 80, 85]
    import pytest
    with pytest.raises(SystemExit):
        map_score(2, 5)                              # 未対応スケールは中止
    assert AUTO_CATEGORIES == {"auto_0", "auto_1", "auto_2", "auto_3"}


def test_build_user_prompt_effort():
    from grader.rubric import ASSIGNMENT_SPECS, build_user_prompt

    text, key = ASSIGNMENT_SPECS["sukina"]
    assert key == "EFFORT"
    # EFFORTルーブリックは採点方針内蔵で stance 差し込みなし・lenient無関係
    p = build_user_prompt(text, key)
    assert "取り組み量" in p and "概念" in p and "識別境界" not in p
    assert "{stance}" not in p
    p2 = build_user_prompt(text, key, lenient=True)
    assert "取り組み量" in p2


# ---- auto_3 / length bonus ----

def _bonus_result(crit_sum=3.0):
    return {"status": "ok", "final_score": 3, "flags": [], "runs": [],
            "judge": {"status": "ok", "runs": [1], "final_score": 3,
                      "crit_min_sum": crit_sum}}


def _bonus_cfg(tmp_path, **overrides):
    lb = {"enabled": True, "rubric_keys": ["KANSOU", "EFFORT"],
          "max_crit_sum": 3.0, "min_crit_sum": 2.5,
          "min_chars": 10, "min_keyword_hits": 2}
    lb.update(overrides)
    return Config({"paths": {"data_dir": str(tmp_path)}, "length_bonus": lb})


def test_length_bonus_disabled(tmp_path):
    cfg = _bonus_cfg(tmp_path, enabled=False)
    assert apply_length_bonus(_bonus_result(), "candidate_3", cfg, "1", "s", "KANSOU") == "candidate_3"


def test_length_bonus_outside_rubric(tmp_path):
    cfg = _bonus_cfg(tmp_path)
    assert apply_length_bonus(_bonus_result(), "candidate_3", cfg, "1", "s", "EXPERIMENT") == "candidate_3"


def test_length_bonus_judge_full_marks(tmp_path):
    r = _bonus_result()
    cat = apply_length_bonus(r, "candidate_3", _bonus_cfg(tmp_path), "1", "s", "KANSOU")
    assert cat == "auto_3" and "judge_full_marks_confirmed" in r["flags"]


@pytest.mark.parametrize("text,flag", [
    ("十分に長い具体的な感想文です", "length_bonus_confirmed"),
    ("SVMとランダムフォレスト", "keyword_bonus_confirmed"),
])
def test_length_or_keyword_bonus(tmp_path, monkeypatch, text, flag):
    monkeypatch.setattr("grader.report._pdf_text", lambda _: text)
    r = _bonus_result(2.5)
    cfg = _bonus_cfg(tmp_path, min_chars=10 if flag.startswith("length") else 100)
    cat = apply_length_bonus(r, "candidate_3", cfg, "1", "s", "KANSOU")
    assert cat == "auto_3" and flag in r["flags"]
