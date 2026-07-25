import json
import stat

import pytest

from grader.config import Config
from grader.settings_templates import (
    MAX_TEMPLATES, SettingsTemplateStore, TemplateScope, TemplateStoreError,
)


def settings(mapping=None):
    return {
        "notes": "採点時の注意",
        "levels": {str(i): f"内部{i}点の条件" for i in range(4)},
        "score_mapping": mapping or {"0": 0, "1": 80, "2": 90, "3": 100},
        "late_penalty": 10,
        "confirmed": True,
    }


def store(tmp_path, scope=None, ids=None):
    identifiers = iter(ids or ["safe_template_id_123456"])
    return SettingsTemplateStore(
        Config({"paths": {"data_dir": str(tmp_path)}}),
        scope or TemplateScope.course("123"),
        clock=lambda: 1_700_000_000,
        id_factory=lambda: next(identifiers),
    )


def test_crud_is_atomic_private_and_list_is_stable(tmp_path):
    service = store(tmp_path, ids=["template_id_b_123456", "template_id_a_123456"])
    second = service.create(name="B", settings=settings(), source_max_points=100)
    first = service.create(name="A", settings=settings(), source_max_points=100)
    assert [item["name"] for item in service.list()] == ["A", "B"]
    assert service.get(first["id"])["updated_at"] == "2023-11-14T22:13:20+00:00"
    updated = service.update(first["id"], name="C", settings=settings(), source_max_points=100)
    assert updated["name"] == "C"
    assert service.delete(second["id"])
    assert not service.delete(second["id"])
    path = tmp_path / "settings_templates" / "course" / "123.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert list(path.parent.glob(".*.tmp")) == []
    assert list(json.loads(path.read_text())["templates"]) == [first["id"]]


def test_ratio_application_preserves_non_linear_mapping_across_max_points(tmp_path):
    service = store(tmp_path)
    saved = service.create(name="非線形", settings=settings(), source_max_points=100)
    assert saved["score_mapping"] == {"0": 0, "1": 0.8, "2": 0.9, "3": 1.0}
    applied = service.apply(saved["id"], max_points=10)
    assert applied["score_mapping"] == {"0": 0, "1": 8, "2": 9, "3": 10}
    assert applied["late_penalty"] == 1
    assert applied["confirmed"] is False
    assert service.apply(saved["id"], max_points=10, confirmed=True)["confirmed"] is True


def test_course_and_owner_scopes_are_isolated_and_owner_is_hashed(tmp_path):
    cfg = Config({"paths": {"data_dir": str(tmp_path)}})
    course = SettingsTemplateStore(cfg, TemplateScope.course("123"), id_factory=lambda: "course_template_123456")
    owner = SettingsTemplateStore(cfg, TemplateScope.owner("raw-google-sub"), id_factory=lambda: "owner_template_123456")
    course.create(name="course", settings=settings(), source_max_points=100)
    owner.create(name="owner", settings=settings(), source_max_points=100)
    assert [item["name"] for item in course.list()] == ["course"]
    assert [item["name"] for item in owner.list()] == ["owner"]
    assert "raw-google-sub" not in str(owner.path)
    assert course.path != owner.path


@pytest.mark.parametrize("bad", ["../123", "abc", "", "1/2"])
def test_course_scope_rejects_path_traversal_and_non_numeric_ids(bad):
    with pytest.raises(ValueError):
        TemplateScope.course(bad)


@pytest.mark.parametrize("bad_id", ["../template", "short", "x" * 65])
def test_template_id_is_strictly_validated(tmp_path, bad_id):
    service = store(tmp_path)
    with pytest.raises(ValueError):
        service.get(bad_id)


def test_existing_settings_validation_and_content_limits_are_reused(tmp_path):
    service = store(tmp_path)
    with pytest.raises(ValueError):
        service.create(name="x" * 81, settings=settings(), source_max_points=100)
    invalid = settings({"0": 0, "1": 90, "2": 80, "3": 100})
    with pytest.raises(ValueError):
        service.create(name="invalid", settings=invalid, source_max_points=100)
    with pytest.raises(ValueError):
        service.create(name="invalid", settings=settings(), source_max_points=float("nan"))


def test_template_count_limit(tmp_path):
    service = SettingsTemplateStore(
        Config({"paths": {"data_dir": str(tmp_path)}}), TemplateScope.course("123"),
        id_factory=(lambda counter=iter(range(MAX_TEMPLATES + 1)): f"template_{next(counter):016d}"),
    )
    for index in range(MAX_TEMPLATES):
        service.create(name=f"template {index}", settings=settings(), source_max_points=100)
    with pytest.raises(ValueError, match="at most"):
        service.create(name="one too many", settings=settings(), source_max_points=100)


def test_corrupt_store_is_not_silently_overwritten(tmp_path):
    service = store(tmp_path)
    service.path.parent.mkdir(parents=True)
    service.path.write_text("not json")
    with pytest.raises(TemplateStoreError):
        service.create(name="safe", settings=settings(), source_max_points=100)
