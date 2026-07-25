"""Idempotency records for MCP-created Google Classroom coursework/announcements.

`subject`と`id_field`を変えることで、課題とお知らせで別々の記録領域を持つ
同じ仕組みを使う。記録は「このMCP利用者が本システムから作成したもの」の
唯一の根拠になるため(お知らせにはassociatedWithDeveloper相当が無い)、
作成完了の記録だけをownedとみなす。
"""
from __future__ import annotations

import hashlib
import json
import pathlib
import re
import threading
import time
from typing import Any, Callable

from .teacher_review import _atomic_json


OWNER_RE = re.compile(r"[0-9a-f]{64}")
KEY_RE = re.compile(r"[A-Za-z0-9_-]{12,128}")


class CourseworkActionStore:
    """Persist create reservations so retries cannot duplicate assignments."""

    def __init__(self, root: pathlib.Path, *, clock: Callable[[], float] = time.time,
                 subject: str = "課題", id_field: str = "coursework_id"):
        self.root = root
        self.clock = clock
        self.subject = subject
        self.id_field = id_field
        self.lock = threading.RLock()

    @staticmethod
    def request_fingerprint(value: dict[str, Any]) -> str:
        packed = json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(packed).hexdigest()

    @staticmethod
    def _owner(value: str) -> str:
        if not OWNER_RE.fullmatch(str(value)):
            raise ValueError("owner reference is invalid")
        return str(value)

    @staticmethod
    def _key(value: str) -> str:
        normalized = str(value).strip()
        if not KEY_RE.fullmatch(normalized):
            raise ValueError("idempotency_keyは英数字・_・-の12〜128文字で指定してください。")
        return normalized

    def _path(self, owner_ref: str, key: str) -> pathlib.Path:
        digest = hashlib.sha256(self._key(key).encode("utf-8")).hexdigest()
        return self.root / self._owner(owner_ref) / f"{digest}.json"

    def created_by_owner(
        self, owner_ref: str, course_id: str, object_id: str,
    ) -> bool:
        """Return whether this owner completed creation of the exact object."""
        owner_dir = self.root / self._owner(owner_ref)
        try:
            paths = list(owner_dir.glob("*.json")) if owner_dir.exists() else []
        except OSError as exc:
            raise RuntimeError(f"{self.subject}作成履歴を確認できません。") from exc
        with self.lock:
            for path in paths:
                value = self._load(path)
                result = value.get("result") if value else None
                if (value and value.get("status") == "completed"
                        and isinstance(result, dict)
                        and result.get("created") is True
                        and str(result.get("course_id")) == str(course_id)
                        and str(result.get(self.id_field)) == str(object_id)):
                    return True
        return False

    def _load(self, path: pathlib.Path) -> dict[str, Any] | None:
        if not path.exists():
            return None
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise RuntimeError(
                f"{self.subject}作成履歴を確認できません。管理者に確認してください。") from exc
        if not isinstance(value, dict):
            raise RuntimeError(f"{self.subject}作成履歴を確認できません。管理者に確認してください。")
        return value

    def begin(
        self, owner_ref: str, key: str, request_fingerprint: str,
    ) -> dict[str, Any] | None:
        """Reserve a key, or return the prior completed result."""
        path = self._path(owner_ref, key)
        with self.lock:
            previous = self._load(path)
            if previous:
                if previous.get("request_fingerprint") != request_fingerprint:
                    raise ValueError(f"同じidempotency_keyを異なる{self.subject}内容には使用できません。")
                if previous.get("status") == "completed" and isinstance(previous.get("result"), dict):
                    return dict(previous["result"])
                raise RuntimeError(
                    f"同じ{self.subject}作成要求が処理中または結果不明です。"
                    "Classroomの下書きを確認してから管理者に相談してください。")
            try:
                _atomic_json(path, {
                    "status": "pending", "request_fingerprint": request_fingerprint,
                    "created_at": int(self.clock()),
                })
            except OSError as exc:
                raise RuntimeError(f"{self.subject}作成履歴を開始できません。") from exc
        return None

    def complete(
        self, owner_ref: str, key: str, request_fingerprint: str, result: dict[str, Any],
    ) -> None:
        path = self._path(owner_ref, key)
        with self.lock:
            current = self._load(path)
            if not current or current.get("request_fingerprint") != request_fingerprint:
                raise RuntimeError(f"{self.subject}作成履歴を更新できません。")
            try:
                _atomic_json(path, {
                    **current, "status": "completed", "completed_at": int(self.clock()),
                    "result": result,
                })
            except OSError as exc:
                raise RuntimeError(f"{self.subject}作成履歴を更新できません。") from exc

    def release(self, owner_ref: str, key: str, request_fingerprint: str) -> None:
        """Release only a request known to have failed before creation."""
        path = self._path(owner_ref, key)
        with self.lock:
            current = self._load(path)
            if current and current.get("request_fingerprint") == request_fingerprint:
                try:
                    path.unlink(missing_ok=True)
                except OSError as exc:
                    raise RuntimeError(f"{self.subject}作成履歴を解放できません。") from exc

    def mark_uncertain(self, owner_ref: str, key: str, request_fingerprint: str) -> None:
        path = self._path(owner_ref, key)
        with self.lock:
            current = self._load(path)
            if current and current.get("request_fingerprint") == request_fingerprint:
                try:
                    _atomic_json(path, {
                        **current, "status": "uncertain", "updated_at": int(self.clock()),
                    })
                except OSError as exc:
                    raise RuntimeError(f"{self.subject}作成履歴を更新できません。") from exc
