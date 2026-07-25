import json

import pytest

from grader.audit import AuditLog, anonymize_actor
from grader.config import Config


def test_append_anonymizes_actor_and_writes_only_allowed_fields(tmp_path):
    log = AuditLog(Config({"paths": {"data_dir": str(tmp_path)}}), clock=lambda: 1234)
    record = log.append(
        actor="google-sub-secret", action="grade.start", course="course-1",
        cw="cw-1", outcome="success",
    )
    assert record == {
        "actor": anonymize_actor("google-sub-secret"),
        "action": "grade.start", "course": "course-1", "cw": "cw-1",
        "outcome": "success", "time": 1234,
    }
    text = (tmp_path / "audit.jsonl").read_text(encoding="utf-8")
    assert "google-sub-secret" not in text
    assert json.loads(text) == record
    assert (tmp_path / "audit.jsonl").stat().st_mode & 0o777 == 0o600


def test_read_limit_returns_latest_records_and_skips_malformed(tmp_path):
    log = AuditLog(Config({}), path=tmp_path / "custom.jsonl", clock=lambda: 100)
    for index in range(3):
        log.append(actor="sub", action=f"action-{index}", outcome="ok")
    with (tmp_path / "custom.jsonl").open("a", encoding="utf-8") as stream:
        stream.write("not-json\n")
    assert [r["action"] for r in log.read(limit=2)] == ["action-1", "action-2"]
    assert log.read(limit=0) == []
    with pytest.raises(ValueError):
        log.read(limit=-1)
