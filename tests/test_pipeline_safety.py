import asyncio
import json

from grader.config import Config
from grader.course_data import CoursePaths
from grader.course_settings import save_settings, settings_fingerprint
from grader.pipeline import grade_all


def setup(tmp_path):
    cfg = Config({"paths": {"data_dir": str(tmp_path)}, "assignments": {}})
    settings = save_settings(cfg, "100", "200", {
        "notes": "n", "levels": {str(i): f"level {i}" for i in range(4)},
        "score_mapping": {str(i): i for i in range(4)}, "confirmed": True,
    }, max_points=3, actor_ref="a" * 64)
    return cfg, CoursePaths(cfg, "100", "200"), settings


def test_returned_submission_is_never_sent_to_renderer(tmp_path, monkeypatch):
    cfg, paths, _ = setup(tmp_path)
    paths.meta.parent.mkdir(parents=True, exist_ok=True)
    paths.meta.write_text(json.dumps({"student_id": "s", "state": "RETURNED"}) + "\n")
    paths.pdf.mkdir(parents=True)
    (paths.pdf / "s.pdf").write_bytes(b"not-a-real-pdf")
    monkeypatch.setattr("grader.pipeline.render_pdf",
                        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("rendered")))
    assert asyncio.run(grade_all(cfg, "200", course_id="100")) == []


def test_format_violation_result_records_settings_fingerprint(tmp_path):
    cfg, paths, settings = setup(tmp_path)
    paths.meta.parent.mkdir(parents=True, exist_ok=True)
    paths.meta.write_text(json.dumps({
        "student_id": "s", "state": "TURNED_IN", "format_violation": True,
    }) + "\n")
    asyncio.run(grade_all(cfg, "200", course_id="100"))
    result = json.loads((paths.results / "s.json").read_text())
    assert result["settings_fingerprint"] == settings_fingerprint(settings)
