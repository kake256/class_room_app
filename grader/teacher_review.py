"""教員確認の永続化と、短期・一回利用の下書き入力バッチ。"""
from __future__ import annotations

import json
import hashlib
import hmac
import os
import pathlib
import secrets
import tempfile
import threading
import time
from typing import Any, Callable

from .config import Config
from .course_data import CoursePaths, numeric_id
from .google_auth import token_reference


def _atomic_json(path: pathlib.Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


class TeacherReviewStore:
    def __init__(self, cfg: Config, *, clock: Callable[[], float] = time.time):
        self.cfg = cfg
        self.clock = clock
        self.lock = threading.RLock()

    def _path(self, owner_sub: str, course_id: str, coursework_id: str) -> pathlib.Path:
        paths = CoursePaths(self.cfg, course_id, coursework_id)
        return paths.root / "teacher_reviews" / f"{token_reference(owner_sub)}.json"

    def _path_reference(self, owner_ref: str, course_id: str, coursework_id: str) -> pathlib.Path:
        if len(owner_ref) != 64 or any(char not in "0123456789abcdef" for char in owner_ref):
            raise ValueError("owner reference is invalid")
        paths = CoursePaths(self.cfg, course_id, coursework_id)
        return paths.root / "teacher_reviews" / f"{owner_ref}.json"

    def load(self, owner_sub: str, course_id: str, coursework_id: str) -> dict[str, dict[str, Any]]:
        return self.load_reference(token_reference(owner_sub), course_id, coursework_id)

    def load_reference(
        self, owner_ref: str, course_id: str, coursework_id: str,
    ) -> dict[str, dict[str, Any]]:
        path = self._path_reference(owner_ref, course_id, coursework_id)
        if not path.exists():
            return {}
        with self.lock:
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return {}
        return raw if isinstance(raw, dict) else {}

    def save(
        self, owner_sub: str, course_id: str, coursework_id: str, student_id: str,
        *, score: float, confirmed: bool, proposal_score: float | None = None,
        signals: list[str] | None = None, review_started_at: float | None = None,
    ) -> dict[str, Any]:
        """教員の確認結果を保存する。

        proposal_score/signalsを渡すと、AI提案からの変更有無・変更幅と、
        確認時に表示されていた注意シグナル(q25_visual_max_score等)を併記する。
        シグナルの妥当性を後から検証するための監査情報であり、点数の自動補正や
        ルーティングには使用しない。
        """
        sid = numeric_id(student_id, "student_id")
        record: dict[str, Any] = {
            "status": "confirmed" if confirmed else "proposal",
            "score": float(score),
            "updated_at": int(self.clock()),
        }
        if proposal_score is not None:
            record["ai_proposal_score"] = float(proposal_score)
            record["score_changed"] = float(score) != float(proposal_score)
            record["score_delta"] = round(float(score) - float(proposal_score), 4)
        if signals is not None:
            record["signals_at_review"] = [str(name)[:64] for name in signals][:20]
        # 人間確認の所要時間。答案本文・学生名・OAuth情報は保存しない。
        if review_started_at is not None:
            started = float(review_started_at)
            completed = float(self.clock())
            duration = completed - started
            # 不正・異常値(未来時刻、8時間超)は所要時間として採用しない。
            if 0 <= duration <= 8 * 3600:
                record["review_started_at"] = int(started)
                record["review_completed_at"] = int(completed)
                record["review_duration_seconds"] = round(duration, 1)
        with self.lock:
            values = self.load(owner_sub, course_id, coursework_id)
            values[sid] = record
            _atomic_json(self._path(owner_sub, course_id, coursework_id), values)
            return values[sid]


class DraftBatchStore:
    """短命capabilityをハッシュだけ保持し、claim→fetch→consumeを管理する。"""
    def __init__(self, *, ttl_seconds: int = 600, clock: Callable[[], float] = time.time):
        self.ttl_seconds = ttl_seconds
        self.clock = clock
        self.lock = threading.Lock()
        self._by_code: dict[str, dict[str, Any]] = {}
        self._by_id: dict[str, dict[str, Any]] = {}
        self._attempts: list[float] = []

    @staticmethod
    def _hash(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    def create(
        self, *, owner_sub: str, course_id: str, coursework_id: str,
        items: list[dict[str, Any]], max_points: float,
    ) -> dict[str, Any]:
        return self.create_reference(
            owner_ref=token_reference(owner_sub), course_id=course_id,
            coursework_id=coursework_id, items=items, max_points=max_points)

    def create_reference(
        self, *, owner_ref: str, course_id: str, coursework_id: str,
        items: list[dict[str, Any]], max_points: float,
    ) -> dict[str, Any]:
        if len(owner_ref) != 64 or any(char not in "0123456789abcdef" for char in owner_ref):
            raise ValueError("owner reference is invalid")
        alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
        code = "".join(secrets.choice(alphabet) for _ in range(12))
        batch = {
            "batch_id": secrets.token_urlsafe(18),
            "owner_ref": owner_ref,
            "course_id": numeric_id(course_id, "course_id"),
            "coursework_id": numeric_id(coursework_id, "coursework_id"),
            "expires_at": int(self.clock()) + self.ttl_seconds,
            "created_ns": time.time_ns(),
            "items": items, "max_points": float(max_points), "status": "ready",
            "token_hash": None,
        }
        with self.lock:
            self._purge()
            self._by_code[self._hash(code)] = batch
            self._by_id[batch["batch_id"]] = batch
        return {"batch_id": batch["batch_id"], "pairing_code": code,
                "course_id": batch["course_id"], "coursework_id": batch["coursework_id"],
                "expires_at": batch["expires_at"], "count": len(items),
                "max_points": batch["max_points"]}

    def claim_ready(
        self, *, owner_ref: str, course_id: str, coursework_id: str,
    ) -> dict[str, Any] | None:
        """Atomically claim the newest owner/context-bound unclaimed batch."""
        selected_course = numeric_id(course_id, "course_id")
        selected_cw = numeric_id(coursework_id, "coursework_id")
        with self.lock:
            self._purge()
            candidates = [batch for batch in self._by_id.values()
                          if batch.get("status") == "ready"
                          and batch.get("owner_ref") == owner_ref
                          and batch.get("course_id") == selected_course
                          and batch.get("coursework_id") == selected_cw]
            if not candidates:
                return None
            batch = max(candidates, key=lambda item: item.get("created_ns", 0))
            token = secrets.token_urlsafe(32)
            batch["token_hash"] = self._hash(token)
            batch["status"] = "claimed"
            self._by_code = {code: value for code, value in self._by_code.items()
                             if value is not batch}
        return {"batch_id": batch["batch_id"], "access_token": token,
                "expires_at": batch["expires_at"]}

    def claim(self, code: str, *, course_id: str, coursework_id: str) -> dict[str, Any] | None:
        normalized = code.strip().upper()
        with self.lock:
            self._purge()
            now = self.clock()
            self._attempts = [stamp for stamp in self._attempts if now - stamp < 60]
            if len(self._attempts) >= 10:
                raise RuntimeError("rate_limited")
            self._attempts.append(now)
            batch = self._by_code.pop(self._hash(normalized), None)
            if not batch:
                return None
            if (batch["course_id"] != numeric_id(course_id, "course_id") or
                    batch["coursework_id"] != numeric_id(coursework_id, "coursework_id")):
                return None
            token = secrets.token_urlsafe(32)
            batch["token_hash"] = self._hash(token)
            batch["status"] = "claimed"
        return {"batch_id": batch["batch_id"], "access_token": token,
                "expires_at": batch["expires_at"]}

    def fetch(self, batch_id: str, token: str) -> dict[str, Any] | None:
        with self.lock:
            self._purge()
            batch = self._by_id.get(batch_id)
            if not batch or batch["status"] not in {"claimed", "delivered"}:
                return None
            if not hmac.compare_digest(batch["token_hash"] or "", self._hash(token)):
                return None
            batch["status"] = "delivered"
            return {key: value for key, value in batch.items()
                    if key not in {"owner_ref", "token_hash", "status"}}

    def consume(self, batch_id: str, token: str) -> bool:
        return self.consume_receipt(batch_id, token) is not None

    def consume_receipt(
        self, batch_id: str, token: str, summary: dict[str, int] | None = None,
    ) -> dict[str, Any] | None:
        with self.lock:
            self._purge()
            batch = self._by_id.get(batch_id)
            if not batch or batch["status"] != "delivered":
                return None
            if not hmac.compare_digest(batch["token_hash"] or "", self._hash(token)):
                return None
            if summary is not None and summary.get("attempted") != len(batch.get("items") or []):
                raise ValueError("attempted does not match batch size")
            receipt = {key: batch[key] for key in (
                "owner_ref", "course_id", "coursework_id", "batch_id",
            )}
            receipt["item_count"] = len(batch.get("items") or [])
            if summary is not None:
                receipt["summary"] = dict(summary)
            batch["status"] = "consumed"
            self._by_id.pop(batch_id, None)
        return receipt

    def _purge(self) -> None:
        now = self.clock()
        expired_ids = {batch["batch_id"] for batch in self._by_id.values()
                       if batch["expires_at"] <= now}
        self._by_id = {bid: batch for bid, batch in self._by_id.items()
                       if bid not in expired_ids}
        self._by_code = {code: batch for code, batch in self._by_code.items()
                         if batch["batch_id"] not in expired_ids}


def prepare_draft_preview(
    rows: list[dict[str, Any]], confirmations: dict[str, dict[str, Any]],
    *, max_points: float,
) -> dict[str, Any]:
    eligible, skipped = [], []
    for row in rows:
        sid = str(row.get("student_id") or "")
        confirmation = confirmations.get(sid) or {}
        reason = None
        manually_confirmed = confirmation.get("status") == "confirmed"
        automatically_eligible = row.get("automatic_eligible") is True and row.get("source") in {
            "system", "external_mcp",
        }
        score = confirmation.get("score") if manually_confirmed else row.get("mapped_score")
        if row.get("state") in {"RETURNED", "NOT_SUBMITTED"} or row.get("category") == "not_submitted":
            reason = "returned_or_not_submitted"
        elif row.get("source") == "human" or any(
            row.get(key) is not None and not (
                isinstance(row.get(key), float) and row.get(key) != row.get(key)
            ) for key in ("assigned_grade", "draft_grade", "existing_grade", "human_score")
        ):
            reason = "existing_or_human_grade"
        elif not manually_confirmed and not automatically_eligible:
            reason = "not_confirmed"
        elif not isinstance(score, (int, float)) or score < 0 or score > max_points:
            reason = "invalid_score"
        if reason:
            skipped.append({"student_id": sid, "reason": reason})
        else:
            eligible.append({"student_id": sid, "score": float(score)})
    return {"eligible": eligible, "skipped": skipped,
            "eligible_count": len(eligible), "skipped_count": len(skipped)}
