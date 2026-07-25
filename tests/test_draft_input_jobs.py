import json
import os

from grader.draft_input_jobs import DraftInputJobStore


OWNER_A = "a" * 64
OWNER_B = "b" * 64


def make_store(tmp_path, now):
    return DraftInputJobStore(tmp_path / "draft_jobs", clock=lambda: now[0],
                              ttl_seconds=300, max_attempts=3)


def test_create_is_idempotent_owner_scoped_and_get_is_read_only(tmp_path):
    now = [1_700_000_000]
    store = make_store(tmp_path, now)
    items = [{"student_id": "student_1", "score": 8}]
    first, created = store.create(OWNER_A, "123", "456", "f" * 64, 10, items)
    repeated, repeated_created = store.create(
        OWNER_A, "123", "456", "f" * 64, 10, items)
    assert created is True and repeated_created is False
    assert repeated["id"] == first["id"] and repeated["status"] == "queued"
    assert store.get(OWNER_A, first["id"])["status"] == "queued"
    assert store.get(OWNER_B, first["id"]) is None
    path = tmp_path / "draft_jobs" / f"{OWNER_A}.json"
    assert oct(os.stat(path).st_mode & 0o777) == "0o600"
    assert json.loads(path.read_text())[0]["owner_ref"] == OWNER_A
    before = path.read_bytes()
    now[0] += 301
    assert store.get(OWNER_A, first["id"])["status"] == "expired"
    assert path.read_bytes() == before


def test_pending_requires_exact_context_and_current_settings(tmp_path):
    now = [1_700_000_000]
    store = make_store(tmp_path, now)
    job, _ = store.create(
        OWNER_A, "123", "456", "f" * 64, 10,
        [{"student_id": "student_1", "score": 8}])
    assert store.pending(OWNER_A, "999", "456", "f" * 64) is None
    assert store.pending(OWNER_A, "123", "999", "f" * 64) is None
    assert store.pending(OWNER_B, "123", "456", "f" * 64) is None
    assert store.pending(OWNER_A, "123", "456", "e" * 64) is None
    stale = store.get(OWNER_A, job["id"])
    assert stale["status"] == "failed" and stale["failure_code"] == "settings_changed"


def test_progress_existing_is_terminal_partial_retry_cancel_and_expiry(tmp_path):
    now = [1_700_000_000]
    store = make_store(tmp_path, now)
    job, _ = store.create(
        OWNER_A, "123", "456", "f" * 64, 10,
        [{"student_id": "one", "score": 8}, {"student_id": "two", "score": 7},
         {"student_id": "three", "score": 6}])
    assert store.pending(OWNER_A, "123", "456", "f" * 64)["status"] == "running"
    progress = store.report(OWNER_A, job["id"], [
        {"student_id": "one", "outcome": "filled"},
        {"student_id": "two", "outcome": "existing"},
        {"student_id": "three", "outcome": "failed"},
    ])
    assert progress["status"] == "partial"
    assert DraftInputJobStore.public(progress)["counts"] == {
        "pending": 1, "filled": 1, "existing": 1, "failed": 0}
    for _ in range(2):
        store.pending(OWNER_A, "123", "456", "f" * 64)
        progress = store.report(OWNER_A, job["id"], [
            {"student_id": "three", "outcome": "failed"}])
    assert progress["status"] == "failed"
    assert DraftInputJobStore.public(progress)["counts"]["failed"] == 1
    retried = store.retry(OWNER_A, job["id"])
    assert retried["status"] == "browser_waiting"
    assert DraftInputJobStore.public(retried)["counts"]["pending"] == 1
    assert store.cancel(OWNER_A, job["id"])["status"] == "canceled"

    expiring, _ = store.create(
        OWNER_A, "123", "999", "f" * 64, 10,
        [{"student_id": "student_4", "score": 5}])
    now[0] += 301
    assert store.get(OWNER_A, expiring["id"])["status"] == "expired"


def test_empty_progress_marks_browser_waiting(tmp_path):
    now = [1_700_000_000]
    store = make_store(tmp_path, now)
    job, _ = store.create(
        OWNER_A, "123", "456", "f" * 64, 10,
        [{"student_id": "one", "score": 8}])
    store.pending(OWNER_A, "123", "456", "f" * 64)
    assert store.report(OWNER_A, job["id"], [])["status"] == "browser_waiting"
