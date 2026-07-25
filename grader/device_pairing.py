"""Persistent extension-device pairing with one-time codes and hash-only tokens."""
from __future__ import annotations

import hashlib
import hmac
import json
import pathlib
import re
import secrets
import threading
import time
from typing import Any, Callable

from .teacher_review import _atomic_json


OWNER_RE = re.compile(r"[0-9a-f]{64}")


class DevicePairingStore:
    def __init__(
        self, root: pathlib.Path, *, clock: Callable[[], float] = time.time,
        code_ttl_seconds: int = 600, rate_limit: int = 10,
    ):
        self.root = root
        self.clock = clock
        self.code_ttl_seconds = min(max(int(code_ttl_seconds), 60), 1800)
        self.rate_limit = max(1, int(rate_limit))
        self.lock = threading.RLock()
        self.pending: dict[str, dict[str, Any]] = {}
        self.attempts: list[float] = []

    @staticmethod
    def _hash(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    @staticmethod
    def _owner(value: str) -> str:
        if not OWNER_RE.fullmatch(value):
            raise ValueError("owner reference is invalid")
        return value

    def _path(self, owner_ref: str) -> pathlib.Path:
        return self.root / f"{self._owner(owner_ref)}.json"

    def _load(self, owner_ref: str) -> list[dict[str, Any]]:
        path = self._path(owner_ref)
        if not path.exists():
            return []
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        return value if isinstance(value, list) else []

    def create_code(self, owner_ref: str, *, label: str, expires_days: int) -> dict[str, Any]:
        owner_ref = self._owner(owner_ref)
        label = str(label).strip() or "Classroom extension"
        if len(label) > 100:
            raise ValueError("端末名は100文字以内です。")
        if isinstance(expires_days, bool) or not isinstance(expires_days, int) or not 1 <= expires_days <= 90:
            raise ValueError("有効日数は1〜90日です。")
        alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
        code = "".join(secrets.choice(alphabet) for _ in range(12))
        expires_at = int(self.clock()) + self.code_ttl_seconds
        with self.lock:
            self._purge_pending()
            self.pending[self._hash(code)] = {
                "owner_ref": owner_ref, "label": label,
                "device_expires_at": int(self.clock()) + expires_days * 86400,
                "code_expires_at": expires_at,
            }
        return {"pairing_code": code, "expires_at": expires_at, "label": label}

    def claim(self, code: str) -> dict[str, Any] | None:
        normalized = str(code).strip().upper()
        if not re.fullmatch(r"[A-Z2-9]{12}", normalized):
            return None
        with self.lock:
            self._purge_pending()
            now = self.clock()
            self.attempts = [stamp for stamp in self.attempts if now - stamp < 60]
            if len(self.attempts) >= self.rate_limit:
                raise RuntimeError("rate_limited")
            self.attempts.append(now)
            pending = self.pending.pop(self._hash(normalized), None)
            if not pending:
                return None
            token = "cgd_" + secrets.token_urlsafe(32)
            record = {
                "id": secrets.token_hex(8), "label": pending["label"],
                "token_sha256": self._hash(token), "created_at": int(now),
                "expires_at": pending["device_expires_at"], "last_used_at": None,
            }
            values = self._load(pending["owner_ref"])
            values.append(record)
            _atomic_json(self._path(pending["owner_ref"]), values)
        return {"device_token": token, "device_id": record["id"],
                "label": record["label"], "expires_at": record["expires_at"]}

    def verify(self, token: str, *, touch: bool = True) -> dict[str, Any] | None:
        if not isinstance(token, str) or not token.startswith("cgd_") or len(token) > 256:
            return None
        digest = self._hash(token)
        now = int(self.clock())
        with self.lock:
            for path in self.root.glob("*.json") if self.root.exists() else []:
                owner_ref = path.stem
                if not OWNER_RE.fullmatch(owner_ref):
                    continue
                values = self._load(owner_ref)
                for record in values:
                    if (record.get("expires_at", 0) > now
                            and hmac.compare_digest(str(record.get("token_sha256", "")), digest)):
                        if touch:
                            record["last_used_at"] = now
                            _atomic_json(path, values)
                        return {"owner_ref": owner_ref, "device_id": record.get("id")}
        return None

    def list_owner(self, owner_ref: str) -> list[dict[str, Any]]:
        now = int(self.clock())
        return [{key: item.get(key) for key in (
            "id", "label", "created_at", "expires_at", "last_used_at",
        )} for item in self._load(owner_ref) if item.get("expires_at", 0) > now]

    def revoke(self, owner_ref: str, device_id: str) -> bool:
        if not re.fullmatch(r"[0-9a-f]{16}", str(device_id)):
            return False
        with self.lock:
            values = self._load(owner_ref)
            kept = [item for item in values if item.get("id") != device_id]
            if len(kept) == len(values):
                return False
            _atomic_json(self._path(owner_ref), kept)
        return True

    def _purge_pending(self) -> None:
        now = self.clock()
        self.pending = {key: value for key, value in self.pending.items()
                        if value["code_expires_at"] > now}
