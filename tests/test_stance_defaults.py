"""ルーブリック種別ごとの既定採点スタンスのテスト(2026-07-20導入)。"""
from grader.rubric import ASSIGNMENT_SPECS, build_user_prompt


def test_kansou_defaults_to_lenient():
    text, key = ASSIGNMENT_SPECS["kansou1"]
    assert key == "KANSOU"
    p = build_user_prompt(text, key)  # lenient未指定
    assert "甘めに採点" in p


def test_kansou_explicit_strict_overrides_default():
    text, key = ASSIGNMENT_SPECS["kansou1"]
    p = build_user_prompt(text, key, lenient=False)
    assert "甘めに採点" not in p and "低い方のスコア" in p


def test_experiment_defaults_to_strict():
    text, key = ASSIGNMENT_SPECS["rf"]
    p = build_user_prompt(text, key)
    assert "甘めに採点" not in p and "低い方のスコア" in p


def test_dedicated_experiment_rubrics_default_to_strict():
    for k in ("distance", "knn"):
        text, key = ASSIGNMENT_SPECS[k]
        p = build_user_prompt(text, key)
        assert "甘めに採点" not in p
