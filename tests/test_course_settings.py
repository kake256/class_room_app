import json
import stat

import pytest

from grader.config import Config
from grader.course_settings import (
    mapped_score, save_settings, settings_fingerprint, settings_prompt, validate_settings,
)
from grader.grade import Grader
from grader.rubric import build_user_prompt


def value():
    return {
        "notes": "課題固有の備考",
        "levels": {str(i): f"{i}点の判断条件" for i in range(4)},
        "score_mapping": {"0": 0, "1": 5, "2": 8, "3": 10},
        "late_penalty": 1,
        "confirmed": True,
    }


def test_settings_validation_prompt_mapping_and_private_save(tmp_path):
    cfg = Config({"paths": {"data_dir": str(tmp_path)}})
    saved = save_settings(cfg, "123", "456", value(), max_points=10,
                          actor_ref="a" * 64)
    path = tmp_path / "courses" / "123" / "courseworks" / "456" / "settings.json"
    assert json.loads(path.read_text())["actor_ref"] == "a" * 64
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert "内部2点: 2点の判断条件" in settings_prompt(saved)
    assert mapped_score(saved, 2, False) == 8
    assert mapped_score(saved, 2, True) == 7


@pytest.mark.parametrize("mapping", [
    {"0": 0, "1": 6, "2": 5, "3": 10},
    {"0": -1, "1": 5, "2": 8, "3": 10},
    {"0": 0, "1": 5, "2": 8, "3": 11},
])
def test_settings_reject_invalid_mapping(mapping):
    candidate = value()
    candidate["score_mapping"] = mapping
    with pytest.raises(ValueError):
        validate_settings(candidate, 10)


def test_mapping_change_does_not_force_regrade_but_levels_change_does():
    original = value()
    mapping_only = value()
    mapping_only["score_mapping"]["2"] = 9
    changed_rule = value()
    changed_rule["levels"]["2"] = "より厳しい2点条件"
    assert settings_fingerprint(original) == settings_fingerprint(mapping_only)
    assert settings_fingerprint(original) != settings_fingerprint(changed_rule)


def test_unregistered_lightweight_assignment_uses_neutral_generic_prompt():
    settings = value()
    grader = Grader(Config({"assignments": {}}), coursework_id="999",
                    lightweight_settings=settings)
    prompt = build_user_prompt(grader.assignment_text, grader.rubric_key) + settings_prompt(settings)
    assert grader.rubric_key == "GENERIC"
    for forbidden in ("RandomForest", "識別境界", "最良パラメータ"):
        assert forbidden not in prompt
    assert "教師が確認済みの課題別採点条件" in prompt
