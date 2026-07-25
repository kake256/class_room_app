import json
import os
import time

import pandas as pd
import pytest

from grader.config import Config
from grader.course_data import CoursePaths
from grader.report import build_report, report_readiness, run_report


def cfg(tmp_path):
    return Config({"paths": {"data_dir": str(tmp_path)},
                   "classroom": {"course_id": "100"}, "assignments": {}})


def write_meta(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))


def write_result(path, sid, score=2):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "status": "ok", "student_id": sid, "final_score": score,
        "runs": [], "flags": [], "graded_at": "now",
    }))


def test_course_paths_isolate_same_coursework_and_default_only_legacy_fallback(tmp_path):
    config = cfg(tmp_path)
    legacy = tmp_path / "report" / "555.csv"
    legacy.parent.mkdir()
    legacy.write_text("category\nauto_2\n")
    assert CoursePaths(config, "100", "555").read_path("report") == legacy
    assert CoursePaths(config, "200", "555").read_path("report") != legacy
    with pytest.raises(ValueError):
        CoursePaths(config, "200", "../555")


def test_readiness_partial_and_atomic_report_preserves_human_grade(tmp_path):
    config = cfg(tmp_path)
    paths = CoursePaths(config, "100", "555")
    write_meta(paths.meta, [
        {"student_id": "h", "state": "RETURNED", "assigned_grade": 9,
         "draft_grade": 8, "name": "human"},
        {"student_id": "s", "state": "TURNED_IN", "name": "system"},
        {"student_id": "m", "state": "TURNED_IN", "name": "missing"},
    ])
    write_result(paths.results / "s.json", "s")
    ready = report_readiness(config, "555", course_id="100")
    assert {key: ready[key] for key in (
        "ready", "total", "human_graded", "system_graded", "missing", "system_target",
        "report_exists",
    )} == {"ready": False, "total": 3, "human_graded": 1,
           "system_graded": 1, "missing": 1, "system_target": 2,
           "report_exists": False}
    assert ready["meta_synced_at"]
    with pytest.raises(RuntimeError, match="未処理"):
        run_report(config, "555", course_id="100")
    out = run_report(config, "555", course_id="100", allow_partial=True)
    df = pd.read_csv(out)
    human = df[df["source"] == "human"].iloc[0]
    assert human["mapped_score"] == 9 and human["category"] == "human"


def test_empty_failure_does_not_overwrite_existing_report(tmp_path):
    config = cfg(tmp_path)
    paths = CoursePaths(config, "100", "777")
    paths.report.parent.mkdir(parents=True)
    paths.report.write_text("keep")
    with pytest.raises(RuntimeError):
        run_report(config, "777", course_id="100")
    assert paths.report.read_text() == "keep"


def test_all_human_report_is_ready_and_allowed(tmp_path):
    config = cfg(tmp_path)
    paths = CoursePaths(config, "100", "888")
    write_meta(paths.meta, [{"student_id": "h", "state": "RETURNED",
                             "draft_grade": 0, "assigned_grade": None}])
    assert report_readiness(config, "888", course_id="100")["ready"] is True
    out = run_report(config, "888", course_id="100")
    assert pd.read_csv(out).iloc[0]["source"] == "human"


def test_returned_without_grade_is_protected_and_stale_meta_blocks(tmp_path):
    config = cfg(tmp_path)
    paths = CoursePaths(config, "100", "999")
    write_meta(paths.meta, [{"student_id": "h", "state": "RETURNED",
                             "draft_grade": None, "assigned_grade": None}])
    ready = report_readiness(config, "999", course_id="100")
    assert ready["human_graded"] == 1 and ready["missing"] == 0
    old = time.time() - 3600
    os.utime(paths.meta, (old, old))
    stale = report_readiness(config, "999", course_id="100")
    assert stale["meta_stale"] is True and stale["ready"] is False
    with pytest.raises(RuntimeError, match="同期情報が古い"):
        run_report(config, "999", course_id="100")
