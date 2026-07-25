"""個人MCP bearer tokenのハッシュ保存と検証。"""
from __future__ import annotations

import hashlib
import json
import os
import pathlib
import secrets
import tempfile
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable

from mcp.server.auth.provider import AccessToken, TokenVerifier


DEFAULT_DAYS = 30
MAX_DAYS = 90


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat()


def _atomic_json(path: pathlib.Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            fd = -1
            json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    except BaseException:
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


class McpTokenStore:
    """Raw tokenを一切永続化しないowner分離token store。"""

    def __init__(
        self, path: pathlib.Path, *, clock: Callable[[], float] = time.time,
        rate_limit: int = 60, rate_window_seconds: int = 60,
    ) -> None:
        self.path = path
        self.clock = clock
        self.rate_limit = max(1, rate_limit)
        self.rate_window_seconds = max(1, rate_window_seconds)
        self.lock = threading.RLock()
        self._attempts: dict[str, list[float]] = {}

    @staticmethod
    def digest(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def _read(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise RuntimeError("MCP token storeを読み取れません。管理者へ連絡してください。") from exc
        if not isinstance(value, list):
            raise RuntimeError("MCP token storeの形式が不正です。管理者へ連絡してください。")
        return value

    @staticmethod
    def _public(record: dict[str, Any]) -> dict[str, Any]:
        return {key: record.get(key) for key in (
            "id", "role", "created_at", "expires_at", "last_used_at",
        )}

    def create(self, owner_ref: str, role: str, days: int = DEFAULT_DAYS) -> dict[str, Any]:
        if days < 1 or days > MAX_DAYS:
            raise ValueError(f"有効日数は1〜{MAX_DAYS}日で指定してください。")
        if role not in {"admin", "grader"}:
            raise PermissionError("採点者権限が必要です。")
        now = self.clock()
        raw = "cga_mcp_" + secrets.token_urlsafe(48)
        record = {
            "id": secrets.token_hex(8),
            "token_sha256": self.digest(raw),
            "owner_ref": owner_ref,
            "role": role,
            "created_at": _iso(now),
            "expires_at": _iso(now + days * 86400),
            "expires_epoch": int(now + days * 86400),
            "last_used_at": None,
        }
        with self.lock:
            records = self._read()
            records.append(record)
            _atomic_json(self.path, records)
        return {"token": raw, "metadata": self._public(record)}

    def list_owner(self, owner_ref: str) -> list[dict[str, Any]]:
        with self.lock:
            return [self._public(item) for item in self._read()
                    if item.get("owner_ref") == owner_ref]

    def revoke(self, owner_ref: str, token_id: str) -> bool:
        with self.lock:
            records = self._read()
            kept = [item for item in records if not (
                item.get("owner_ref") == owner_ref and item.get("id") == token_id
            )]
            if len(kept) == len(records):
                return False
            _atomic_json(self.path, kept)
            return True

    def verify(self, raw: str) -> dict[str, Any] | None:
        if not raw.startswith("cga_mcp_") or len(raw) < 60 or len(raw) > 128:
            return None
        digest = self.digest(raw)
        now = self.clock()
        with self.lock:
            attempts = [stamp for stamp in self._attempts.get(digest, [])
                        if now - stamp < self.rate_window_seconds]
            if len(attempts) >= self.rate_limit:
                return None
            attempts.append(now)
            self._attempts[digest] = attempts
            records = self._read()
            found = next((item for item in records
                          if secrets.compare_digest(str(item.get("token_sha256", "")), digest)), None)
            if found is None or int(found.get("expires_epoch", 0)) <= int(now):
                return None
            found["last_used_at"] = _iso(now)
            _atomic_json(self.path, records)
            return dict(found)


class McpBearerVerifier(TokenVerifier):
    def __init__(self, store: McpTokenStore) -> None:
        self.store = store

    async def verify_token(self, token: str) -> AccessToken | None:
        record = self.store.verify(token)
        if record is None:
            return None
        return AccessToken(
            token="verified", client_id=str(record["owner_ref"]),
            scopes=[str(record["role"])], expires_at=int(record["expires_epoch"]),
            subject=str(record["id"]),
        )
