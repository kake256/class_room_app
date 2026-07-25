import json

import pytest

from grader.coursework_actions import CourseworkActionStore


def test_creation_key_is_idempotent_and_payload_bound(tmp_path):
    store = CourseworkActionStore(tmp_path, clock=lambda: 1700000000)
    owner = "a" * 64
    fingerprint = store.request_fingerprint({"course_id": "123", "title": "Draft"})
    assert store.begin(owner, "request_key_123", fingerprint) is None
    with pytest.raises(RuntimeError):
        store.begin(owner, "request_key_123", fingerprint)
    result = {"coursework_id": "456", "state": "DRAFT"}
    store.complete(owner, "request_key_123", fingerprint, result)
    assert store.begin(owner, "request_key_123", fingerprint) == result
    with pytest.raises(ValueError):
        store.begin(owner, "request_key_123", "f" * 64)
    saved = list((tmp_path / owner).glob("*.json"))
    assert len(saved) == 1
    assert "request_key_123" not in saved[0].read_text(encoding="utf-8")
    assert json.loads(saved[0].read_text(encoding="utf-8"))["status"] == "completed"


def test_known_failure_releases_but_uncertain_result_blocks_retry(tmp_path):
    store = CourseworkActionStore(tmp_path)
    owner, fingerprint = "b" * 64, "f" * 64
    assert store.begin(owner, "known_failure_1", fingerprint) is None
    store.release(owner, "known_failure_1", fingerprint)
    assert store.begin(owner, "known_failure_1", fingerprint) is None
    store.mark_uncertain(owner, "known_failure_1", fingerprint)
    with pytest.raises(RuntimeError):
        store.begin(owner, "known_failure_1", fingerprint)


def test_created_by_owner_requires_completed_creation_result(tmp_path):
    store = CourseworkActionStore(tmp_path)
    owner = "c" * 64
    fingerprint = store.request_fingerprint({"course_id": "123", "title": "Draft"})
    store.begin(owner, "creation_record_001", fingerprint)
    store.complete(owner, "creation_record_001", fingerprint, {
        "created": True, "course_id": "123", "coursework_id": "456", "state": "DRAFT",
    })
    assert store.created_by_owner(owner, "123", "456") is True
    assert store.created_by_owner(owner, "123", "999") is False
    assert store.created_by_owner("d" * 64, "123", "456") is False


@pytest.mark.parametrize("key", ["short", "spaces are bad", "x" * 129])
def test_creation_key_validation(tmp_path, key):
    with pytest.raises(ValueError):
        CourseworkActionStore(tmp_path).begin("a" * 64, key, "f" * 64)
