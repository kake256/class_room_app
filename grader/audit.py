"""個人情報を持たない追記専用の操作監査ログ。"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import pathlib
import time
from typing import Any, Callable

from .config import Config

_FIELDS = ("actor", "action", "course", "cw", "outcome", "time")


def anonymize_actor(actor: str) -> str:
    """Google sub等の識別子をログに直接保存しない安定した匿名IDへ変換する。"""
    return hashlib.sha256(actor.encode("utf-8")).hexdigest()[:24]


class AuditLog:
    def __init__(
        self,
        cfg: Config,
        *,
        clock: Callable[[], float] = time.time,
        path: str | pathlib.Path | None = None,
    ):
        configured = path or cfg.get("audit", "path", default=None)
        self.path = pathlib.Path(configured) if configured else cfg.data_dir / "audit.jsonl"
        self.clock = clock

    def append(
        self,
        *,
        actor: str,
        action: str,
        course: str | None = None,
        cw: str | None = None,
        outcome: str,
    ) -> dict[str, Any]:
        record = {
            "actor": anonymize_actor(actor),
            "action": str(action),
            "course": str(course) if course is not None else None,
            "cw": str(cw) if cw is not None else None,
            "outcome": str(outcome),
            "time": int(self.clock()),
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.chmod(self.path, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX)
            data = (json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
            view = memoryview(data)
            while view:
                view = view[os.write(fd, view):]
            os.fsync(fd)
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)
        return record

    def read(self, *, limit: int = 100) -> list[dict[str, Any]]:
        if limit < 0:
            raise ValueError("limit must be non-negative")
        if limit == 0 or not self.path.exists():
            return []
        fd = os.open(self.path, os.O_RDONLY)
        try:
            fcntl.flock(fd, fcntl.LOCK_SH)
            with os.fdopen(os.dup(fd), encoding="utf-8") as stream:
                lines = stream.readlines()
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)
        records: list[dict[str, Any]] = []
        for line in reversed(lines):
            try:
                item = json.loads(line)
            except (json.JSONDecodeError, TypeError):
                continue
            if isinstance(item, dict) and tuple(item.keys()) == _FIELDS:
                records.append(item)
                if len(records) == limit:
                    break
        records.reverse()
        return records
