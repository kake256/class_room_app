"""Persistent owner-scoped jobs for automatic Classroom draft input."""
from __future__ import annotations

import pathlib
import re
import secrets
import threading
import time
from typing import Any, Callable

from .teacher_review import _atomic_json


ACTIVE = {"queued", "browser_waiting", "running", "partial"}
TERMINAL = {"succeeded", "failed", "canceled", "expired"}


class DraftInputJobStore:
    def __init__(self, root: pathlib.Path, *, clock: Callable[[], float] = time.time,
                 ttl_seconds: int = 3600, max_attempts: int = 3):
        self.root = root
        self.clock = clock
        self.ttl_seconds = max(300, min(int(ttl_seconds), 86400))
        self.max_attempts = max(1, min(int(max_attempts), 10))
        self.lock = threading.RLock()

    def _path(self, owner_ref: str) -> pathlib.Path:
        if not re.fullmatch(r"[0-9a-f]{64}", owner_ref):
            raise ValueError("owner reference is invalid")
        return self.root / f"{owner_ref}.json"

    def _load(self, owner_ref: str) -> list[dict[str, Any]]:
        path = self._path(owner_ref)
        if not path.exists():
            return []
        try:
            import json
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        return value if isinstance(value, list) else []

    def _expire(self, jobs: list[dict[str, Any]]) -> bool:
        now, changed = int(self.clock()), False
        for job in jobs:
            if job.get("status") in ACTIVE and job.get("expires_at", 0) <= now:
                job["status"], job["updated_at"] = "expired", now
                changed = True
        return changed

    def create(self, owner_ref: str, course_id: str, coursework_id: str,
               fingerprint: str, max_points: float,
               items: list[dict[str, Any]]) -> tuple[dict[str, Any], bool]:
        if not fingerprint or not 1 <= len(items) <= 500:
            raise ValueError("下書き入力対象は1〜500件です。")
        now = int(self.clock())
        with self.lock:
            jobs = self._load(owner_ref); changed = self._expire(jobs)
            for job in jobs:
                if (job.get("status") in ACTIVE and job.get("course_id") == course_id
                        and job.get("coursework_id") == coursework_id
                        and job.get("settings_fingerprint") == fingerprint):
                    if changed: _atomic_json(self._path(owner_ref), jobs)
                    return job, False
            job = {
                "id": secrets.token_hex(6), "owner_ref": owner_ref,
                "course_id": course_id, "coursework_id": coursework_id,
                "settings_fingerprint": fingerprint, "max_points": float(max_points),
                "status": "queued", "created_at": now, "updated_at": now,
                "expires_at": now + self.ttl_seconds,
                "items": [{"student_id": str(item["student_id"]),
                           "student_name": str(item.get("student_name") or ""),
                           "score": float(item["score"]), "status": "pending",
                           "attempts": 0} for item in items],
            }
            jobs.append(job); _atomic_json(self._path(owner_ref), jobs)
            return job, True

    def get(self, owner_ref: str, job_id: str) -> dict[str, Any] | None:
        with self.lock:
            jobs = self._load(owner_ref)
            job = next((item for item in jobs if item.get("id") == job_id), None)
            if (job and job.get("status") in ACTIVE
                    and job.get("expires_at", 0) <= int(self.clock())):
                return {**job, "status": "expired", "updated_at": int(self.clock())}
            return job

    def pending(self, owner_ref: str, course_id: str, coursework_id: str,
                fingerprint: str) -> dict[str, Any] | None:
        now = int(self.clock())
        with self.lock:
            jobs = self._load(owner_ref); changed = self._expire(jobs)
            candidates = [job for job in jobs if job.get("status") in ACTIVE
                          and job.get("course_id") == course_id
                          and job.get("coursework_id") == coursework_id]
            job = min(candidates, key=lambda item: item["created_at"]) if candidates else None
            if job and job.get("settings_fingerprint") != fingerprint:
                job["status"], job["failure_code"], job["updated_at"] = (
                    "failed", "settings_changed", now)
                changed, job = True, None
            elif job:
                job["status"], job["updated_at"] = "running", now
                changed = True
            if changed: _atomic_json(self._path(owner_ref), jobs)
            return job

    def report(self, owner_ref: str, job_id: str,
               results: list[dict[str, str]]) -> dict[str, Any] | None:
        now = int(self.clock())
        with self.lock:
            jobs = self._load(owner_ref); self._expire(jobs)
            job = next((item for item in jobs if item.get("id") == job_id), None)
            if not job or job.get("status") not in ACTIVE:
                return None
            by_id = {item["student_id"]: item for item in job["items"]}
            for result in results:
                item = by_id.get(str(result.get("student_id")))
                outcome = result.get("outcome")
                if not item or item["status"] != "pending" or outcome not in {"filled", "existing", "failed"}:
                    raise ValueError("invalid progress result")
                item["attempts"] += 1
                if outcome in {"filled", "existing"}:
                    item["status"] = outcome
                elif item["attempts"] >= self.max_attempts:
                    item["status"] = "failed"
            pending = sum(item["status"] == "pending" for item in job["items"])
            failed = sum(item["status"] == "failed" for item in job["items"])
            completed = len(job["items"]) - pending - failed
            if pending:
                job["status"] = "partial" if completed or failed else "browser_waiting"
            else:
                job["status"] = "failed" if failed else "succeeded"
            job["updated_at"] = now
            _atomic_json(self._path(owner_ref), jobs)
            return job

    def retry(self, owner_ref: str, job_id: str) -> dict[str, Any] | None:
        with self.lock:
            jobs = self._load(owner_ref); self._expire(jobs)
            job = next((item for item in jobs if item.get("id") == job_id), None)
            if not job or job.get("status") not in {"partial", "failed", "browser_waiting"}:
                return None
            for item in job["items"]:
                if item["status"] == "failed":
                    item["status"], item["attempts"] = "pending", 0
            job["status"], job["updated_at"] = "browser_waiting", int(self.clock())
            job["expires_at"] = int(self.clock()) + self.ttl_seconds
            _atomic_json(self._path(owner_ref), jobs)
            return job

    def cancel(self, owner_ref: str, job_id: str) -> dict[str, Any] | None:
        with self.lock:
            jobs = self._load(owner_ref); self._expire(jobs)
            job = next((item for item in jobs if item.get("id") == job_id), None)
            if not job or job.get("status") not in ACTIVE:
                return None
            job["status"], job["updated_at"] = "canceled", int(self.clock())
            _atomic_json(self._path(owner_ref), jobs)
            return job

    @staticmethod
    def public(job: dict[str, Any]) -> dict[str, Any]:
        counts = {state: sum(item["status"] == state for item in job["items"])
                  for state in ("pending", "filled", "existing", "failed")}
        return {key: job.get(key) for key in (
            "id", "course_id", "coursework_id", "settings_fingerprint", "status",
            "created_at", "updated_at", "expires_at", "failure_code",
        )} | {"counts": counts, "total": len(job["items"]), "classroom_finalized": False}
