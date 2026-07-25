import time

import pytest

from grader.config import Config
from grader.course_overview import compose_course_overview


def config():
    return Config({"assignments": {"2": "legacy"}})


def test_bulk_composition_preserves_order_and_calls_each_loader_once():
    settings_calls, readiness_calls = [], []

    def settings_loader(cfg, course_id, coursework_id):
        settings_calls.append((course_id, coursework_id))
        return {"confirmed": coursework_id == "1", "notes": "safe"} if coursework_id != "3" else None

    def readiness_loader(cfg, course_id, coursework_id):
        readiness_calls.append((course_id, coursework_id))
        return {
            "ready": True, "total": int(coursework_id), "human_graded": 0,
            "system_graded": 0, "missing": 0, "system_target": 0,
            "report_exists": False, "meta_synced_at": None, "meta_stale": False,
            "meta_age_seconds": 0,
            "students": [{"name": "must not leak"}],
        }

    source = [
        {"id": "3", "title": "third", "extra": {"student_name": "drop"}},
        {"id": "1", "title": "first"},
        {"id": "2", "title": "second"},
    ]
    result = compose_course_overview(
        config(), "100", source,
        settings_loader=settings_loader, readiness_loader=readiness_loader,
    )
    rows = result["courseworks"]
    assert [row["id"] for row in rows] == ["3", "1", "2"]
    assert settings_calls == [("100", "3"), ("100", "1"), ("100", "2")]
    assert readiness_calls == settings_calls
    assert rows[0]["readiness"]["total"] == 3
    assert "students" not in rows[0]["readiness"]
    assert "extra" not in rows[0]
    assert rows[0]["configured"] is False
    assert rows[1]["configured"] is True
    # Existing unconfirmed settings override the legacy mapping, matching the UI.
    assert rows[2]["assignment_key"] == "legacy"
    assert rows[2]["configured"] is False


def test_missing_data_preserves_existing_none_and_zero_count_semantics(tmp_path):
    cfg = Config({"paths": {"data_dir": str(tmp_path)}, "assignments": {}})
    result = compose_course_overview(cfg, "100", [{"id": "200", "title": "empty"}])
    row = result["courseworks"][0]
    assert row["settings"] is None
    assert row["configured"] is False
    assert row["readiness"]["total"] == 0
    assert row["readiness"]["human_graded"] == 0
    assert row["readiness"]["system_graded"] == 0
    assert row["readiness"]["missing"] == 0
    assert row["readiness"]["report_exists"] is False
    assert row["readiness"]["meta_stale"] is True
    assert row["overview_errors"] == []


def test_errors_are_isolated_without_exception_or_pii_disclosure():
    settings_calls, readiness_calls = [], []

    def settings_loader(cfg, course_id, coursework_id):
        settings_calls.append(coursework_id)
        if coursework_id == "2":
            raise OSError("/private/student-name/settings.json")
        return {"confirmed": True}

    def readiness_loader(cfg, course_id, coursework_id):
        readiness_calls.append(coursework_id)
        if coursework_id == "3":
            raise ValueError("student@example.com")
        return {"ready": True, "total": 1}

    rows = compose_course_overview(
        config(), "100", [{"id": str(i)} for i in range(1, 5)],
        settings_loader=settings_loader, readiness_loader=readiness_loader,
    )["courseworks"]
    assert rows[0]["overview_errors"] == []
    assert rows[1]["overview_errors"] == [{"code": "settings_unavailable"}]
    assert rows[1]["readiness"]["total"] == 1
    assert rows[2]["overview_errors"] == [{"code": "readiness_unavailable"}]
    assert rows[2]["settings"] == {"confirmed": True}
    assert rows[3]["overview_errors"] == []
    serialized = repr(rows)
    assert "student-name" not in serialized and "student@example.com" not in serialized
    assert settings_calls == ["1", "2", "3", "4"]
    assert readiness_calls == ["1", "2", "3", "4"]


def test_100_courseworks_complete_in_reasonable_domain_time():
    calls = {"settings": 0, "readiness": 0}

    def settings_loader(*_):
        calls["settings"] += 1
        return None

    def readiness_loader(*_):
        calls["readiness"] += 1
        return {"ready": False, "total": 0}

    started = time.perf_counter()
    result = compose_course_overview(
        config(), "100", [{"id": str(index), "title": str(index)} for index in range(1, 101)],
        settings_loader=settings_loader, readiness_loader=readiness_loader,
    )
    elapsed = time.perf_counter() - started
    assert len(result["courseworks"]) == 100
    assert calls == {"settings": 100, "readiness": 100}
    assert elapsed < 1.0


@pytest.mark.parametrize("course_id,courseworks", [
    ("../100", [{"id": "1"}]),
    ("100", [{"id": "../1"}]),
    ("100", [{"id": "1"}, {"id": "1"}]),
])
def test_invalid_or_duplicate_ids_are_rejected_before_loading(course_id, courseworks):
    called = []
    with pytest.raises(ValueError):
        compose_course_overview(
            config(), course_id, courseworks,
            settings_loader=lambda *_: called.append("settings"),
            readiness_loader=lambda *_: called.append("readiness"),
        )
    assert called == []
