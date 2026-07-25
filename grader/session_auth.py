"""Google identityに紐づく署名済みWebセッション。"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import fcntl
import json
import logging
import os
import secrets
import time
from dataclasses import dataclass
from typing import Callable

from .config import Config

log = logging.getLogger(__name__)
COOKIE_NAME = "cga_session"


class InvalidSession(ValueError):
    pass


class UserNotAllowed(PermissionError):
    pass


@dataclass(frozen=True)
class SessionIdentity:
    sub: str
    email: str
    csrf_token: str
    role: str


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


class SessionManager:
    def __init__(self, cfg: Config, *, clock: Callable[[], float] = time.time):
        self.cfg = cfg
        self.clock = clock
        configured = os.environ.get("CGA_SESSION_SECRET") or cfg.get(
            "web_auth", "session_secret", default=None
        )
        if configured:
            self._secret = str(configured).encode("utf-8")
            if len(self._secret) < 32:
                raise ValueError("web_auth.session_secret/CGA_SESSION_SECRETは32バイト以上が必要です")
        else:
            self._secret = self._load_or_create_secret()
            log.warning("Webセッション秘密鍵が未設定のため、data配下の永続秘密鍵を使用します。")

        self.allowed_emails = {
            str(v).strip().lower() for v in (cfg.get("web_auth", "allowed_emails", default=[]) or [])
            if str(v).strip()
        }
        self.allowed_domains = {
            str(v).strip().lower().lstrip("@")
            for v in (cfg.get("web_auth", "allowed_domains", default=[]) or [])
            if str(v).strip().lstrip("@")
        }
        self.roles: dict[str, tuple[set[str], set[str]]] = {}
        for role in ("admin", "grader", "viewer"):
            role_cfg = cfg.get("web_auth", "roles", role, default={}) or {}
            if not isinstance(role_cfg, dict):
                role_cfg = {}
            emails = {
                str(v).strip().lower() for v in (role_cfg.get("emails", []) or [])
                if str(v).strip()
            }
            domains = {
                str(v).strip().lower().lstrip("@")
                for v in (role_cfg.get("domains", []) or [])
                if str(v).strip().lstrip("@")
            }
            self.roles[role] = (emails, domains)
        self._explicit_roles = any(emails or domains for emails, domains in self.roles.values())
        self.require_explicit_roles = bool(cfg.get(
            "web_auth", "require_explicit_roles", default=False))
        if not self._explicit_roles and not self.allowed_emails and not self.allowed_domains:
            log.warning("Googleログイン許可リストが空です。OAuthクライアントで許可された全ユーザーを許可します。")

    def _load_or_create_secret(self) -> bytes:
        path = self.cfg.data_dir / "session_secret"
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            os.chmod(path, 0o600)
            secret = os.read(fd, 4096)
            if not secret:
                secret = secrets.token_bytes(32)
                view = memoryview(secret)
                while view:
                    view = view[os.write(fd, view):]
                os.fsync(fd)
            elif len(secret) < 32:
                raise ValueError(f"Webセッション秘密鍵が短すぎます: {path}")
            return secret
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    @property
    def ttl_seconds(self) -> int:
        return int(self.cfg.get("web_auth", "session_ttl_seconds", default=43200))

    @property
    def secure_cookie(self) -> bool:
        return bool(self.cfg.get("web_auth", "secure_cookie", default=False))

    def authorize(self, email: str) -> None:
        normalized = email.strip().lower()
        domain = normalized.rsplit("@", 1)[1] if "@" in normalized else ""
        if self.require_explicit_roles and not self._explicit_roles:
            raise UserNotAllowed("本番モードでは明示的な利用者ロール設定が必要です。")
        if self._explicit_roles:
            if self._match_role(normalized, domain) is None:
                raise UserNotAllowed("このGoogleアカウントには利用権限がありません。")
            return
        if self.allowed_emails or self.allowed_domains:
            if normalized not in self.allowed_emails and domain not in self.allowed_domains:
                raise UserNotAllowed("このGoogleアカウントには利用権限がありません。")

    def _match_role(self, email: str, domain: str) -> str | None:
        for role in ("admin", "grader", "viewer"):
            emails, domains = self.roles[role]
            if email in emails or domain in domains:
                return role
        return None

    def resolve_role(self, email: str) -> str:
        """設定された役割を admin > grader > viewer の順で解決する。"""
        normalized = email.strip().lower()
        domain = normalized.rsplit("@", 1)[1] if "@" in normalized else ""
        role = self._match_role(normalized, domain)
        if role is not None:
            return role
        if not self._explicit_roles:
            # 既存設定との開発互換: 従来のallowlist（空なら全OAuth利用者）をgrader扱い。
            self.authorize(normalized)
            return "grader"
        raise UserNotAllowed("このGoogleアカウントには利用権限がありません。")

    def create(self, *, sub: str, email: str) -> tuple[str, SessionIdentity]:
        self.authorize(email)
        role = self.resolve_role(email)
        now = int(self.clock())
        csrf = secrets.token_urlsafe(24)
        payload = {
            "sub": sub,
            "email": email.strip().lower(),
            "iat": now,
            "exp": now + self.ttl_seconds,
            "csrf": csrf,
            "role": role,
        }
        body = _b64encode(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8"))
        signature = _b64encode(hmac.new(self._secret, body.encode("ascii"), hashlib.sha256).digest())
        return f"{body}.{signature}", SessionIdentity(sub, payload["email"], csrf, role)

    def verify(self, token: str | None) -> SessionIdentity:
        if not token:
            raise InvalidSession("ログインが必要です。")
        try:
            body, supplied_signature = token.split(".", 1)
            expected = hmac.new(self._secret, body.encode("ascii"), hashlib.sha256).digest()
            if not hmac.compare_digest(expected, _b64decode(supplied_signature)):
                raise InvalidSession("セッションが不正です。")
            payload = json.loads(_b64decode(body))
            if int(payload["exp"]) <= int(self.clock()):
                raise InvalidSession("セッションの有効期限が切れました。")
            sub, email, csrf = payload["sub"], payload["email"], payload["csrf"]
            if not all(isinstance(v, str) and v for v in (sub, email, csrf)):
                raise InvalidSession("セッションが不正です。")
            self.authorize(email)
            # 設定変更は既存Cookieにも即時反映する。payloadのroleは信頼しない。
            role = self.resolve_role(email)
            return SessionIdentity(sub, email, csrf, role)
        except (ValueError, KeyError, TypeError, binascii.Error, json.JSONDecodeError) as exc:
            if isinstance(exc, InvalidSession):
                raise
            raise InvalidSession("セッションが不正です。") from exc
