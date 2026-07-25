"""Web UI から行う Google Classroom OAuth 接続。

Web利用者のトークンはGoogle identity ``sub`` のSHA-256ハッシュごとに
data配下へ分離保存する。OAuth stateはプロセス内に短時間だけ保持する。
"""
from __future__ import annotations

import errno
import hashlib
import json
import os
import pathlib
import re
import secrets
import tempfile
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

from .config import Config
from .fetch import SCOPES


DEFAULT_REDIRECT_URI = "http://localhost:8800/oauth2callback"
DEFAULT_STATE_TTL = 600
_BIND_MOUNT_FALLBACK_ERRNOS = {
    errno.EXDEV, errno.EBUSY, errno.EPERM, errno.EACCES, errno.EROFS,
}


class OAuthConfigurationError(RuntimeError):
    """OAuthクライアント設定を利用できない。"""


class OAuthStateError(RuntimeError):
    """state が不正または期限切れ。"""


@dataclass(frozen=True)
class PendingOAuth:
    created_at: float
    code_verifier: str | None
    redirect_uri: str
    return_to: str | None = None


@dataclass(frozen=True)
class GoogleIdentity:
    sub: str
    email: str


OAUTH_SCOPES = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    *SCOPES,
    # Web UIの明示操作によるランキング出力専用。CLI用SCOPESには追加しない。
    "https://www.googleapis.com/auth/spreadsheets",
    # MCPの明示確認によるお知らせ下書き作成・公開専用。CLI用SCOPESには追加しない。
    "https://www.googleapis.com/auth/classroom.announcements",
]
SHEETS_SCOPE = "https://www.googleapis.com/auth/spreadsheets"
ANNOUNCEMENTS_SCOPE = "https://www.googleapis.com/auth/classroom.announcements"
# 既存tokenに後から追加したscope。欠けていれば再同意を求める。
ADDED_SCOPES = (SHEETS_SCOPE, ANNOUNCEMENTS_SCOPE)


def token_reference(sub: str) -> str:
    """Google subを保存パス/ジョブに漏らさない安定参照へ変換。"""
    return hashlib.sha256(sub.encode("utf-8")).hexdigest()


def write_token_atomic(path: pathlib.Path, value: str) -> None:
    """OAuth tokenを0600で可能な限り原子的に保存する。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    except OSError as exc:
        if exc.errno not in _BIND_MOUNT_FALLBACK_ERRNOS:
            raise
        _write_token_compat(path, value)
        return
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            fd = -1
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.replace(temporary, path)
            os.chmod(path, 0o600)
        except OSError as exc:
            if exc.errno not in _BIND_MOUNT_FALLBACK_ERRNOS:
                raise
            _write_token_compat(path, value)
            os.unlink(temporary)
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


def _write_token_compat(path: pathlib.Path, value: str) -> None:
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT, 0o600)
        try:
            os.fchmod(fd, 0o600)
            os.ftruncate(fd, 0)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                fd = -1
                handle.write(value)
                handle.flush()
                os.fsync(handle.fileno())
        finally:
            if fd >= 0:
                os.close(fd)
    except OSError as exc:
        raise OAuthConfigurationError(
            "OAuthトークンを保存できません。管理者がトークン保存先の書込権限を確認してください。"
        ) from exc


class GoogleOAuthManager:
    def __init__(self, cfg: Config, *, clock: Callable[[], float] = time.time):
        self.cfg = cfg
        self.clock = clock
        self._pending: dict[str, PendingOAuth] = {}
        self._lock = threading.Lock()

    @property
    def credentials_path(self) -> pathlib.Path:
        return pathlib.Path(self.cfg.get("classroom", "credentials_file", default="credentials.json"))

    @property
    def token_path(self) -> pathlib.Path:
        """CLI後方互換用の共有token。Web OAuthでは使用しない。"""
        return pathlib.Path(self.cfg.get("classroom", "token_file", default="token.json"))

    @property
    def user_token_dir(self) -> pathlib.Path:
        configured = self.cfg.get("classroom", "user_token_dir", default=None)
        return pathlib.Path(configured) if configured else self.cfg.data_dir / "oauth_tokens"

    def user_token_path(self, sub: str) -> pathlib.Path:
        return self.user_token_dir / f"{token_reference(sub)}.json"

    def token_path_for_reference(self, reference: str) -> pathlib.Path:
        if len(reference) != 64 or any(ch not in "0123456789abcdef" for ch in reference):
            raise OAuthConfigurationError("利用者トークン参照が不正です。")
        return self.user_token_dir / f"{reference}.json"

    @property
    def redirect_uri(self) -> str:
        return str(self.cfg.get("classroom", "oauth_redirect_uri", default=DEFAULT_REDIRECT_URI))

    @property
    def state_ttl(self) -> int:
        return int(self.cfg.get("classroom", "oauth_state_ttl_seconds", default=DEFAULT_STATE_TTL))

    def _client_config(self) -> tuple[dict[str, Any], str]:
        path = self.credentials_path
        if not path.exists() or path.stat().st_size == 0:
            raise OAuthConfigurationError(
                "OAuthクライアントJSONがありません。管理者がcredentials_fileを配置してください。"
            )
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise OAuthConfigurationError("OAuthクライアントJSONを読み取れません。") from exc
        client_type = "web" if isinstance(data.get("web"), dict) else (
            "installed" if isinstance(data.get("installed"), dict) else "unknown"
        )
        if client_type == "unknown":
            raise OAuthConfigurationError("OAuthクライアントJSONの形式が不正です。")
        return data, client_type

    def _client_id(self, client_config: dict[str, Any], client_type: str) -> str:
        client_id = client_config[client_type].get("client_id")
        if not client_id:
            raise OAuthConfigurationError("OAuthクライアントIDが設定されていません。")
        return str(client_id)

    def status(self, sub: str | None = None) -> dict[str, Any]:
        out: dict[str, Any] = {
            "credentials_present": False,
            "credentials_valid": False,
            "client_type": None,
            "token_present": False,
            "token_parseable": False,
            "token_valid": False,
            "token_expired": False,
            "has_refresh_token": False,
            "connected": False,
            "needs_reconnect": False,
            "sheets_scope_granted": False,
            "announcements_scope_granted": False,
            "redirect_uri": self.redirect_uri,
        }
        try:
            _, client_type = self._client_config()
            out.update(credentials_present=True, credentials_valid=True, client_type=client_type)
        except OAuthConfigurationError:
            path = self.credentials_path
            out["credentials_present"] = path.exists() and path.stat().st_size > 0

        path = self.user_token_path(sub) if sub else self.token_path
        out["token_present"] = path.exists() and path.stat().st_size > 0
        if out["token_present"]:
            try:
                from google.oauth2.credentials import Credentials

                # 要求scopeを渡すと旧tokenまで新scopeを持つように見えるため、
                # 保存済みtoken自身のscopeを検査する。
                creds = Credentials.from_authorized_user_file(str(path))
                out.update(
                    token_parseable=True,
                    token_valid=bool(creds.valid),
                    token_expired=bool(creds.expired),
                    has_refresh_token=bool(creds.refresh_token),
                    sheets_scope_granted=bool(creds.has_scopes([SHEETS_SCOPE])),
                    announcements_scope_granted=bool(
                        creds.has_scopes([ANNOUNCEMENTS_SCOPE])),
                )
            except Exception:  # noqa: BLE001 壊れた/旧形式tokenは状態表示で失敗させない
                pass
        # 期限切れでもrefresh tokenがあれば、API利用時に自動更新できる。
        out["connected"] = bool(
            out["credentials_valid"]
            and out["token_parseable"]
            and (out["token_valid"] or (out["token_expired"] and out["has_refresh_token"]))
        )
        out["needs_reconnect"] = bool(
            out["credentials_valid"]
            and (not out["connected"] or not out["sheets_scope_granted"]
                 or not out["announcements_scope_granted"])
        )
        return out

    def user_credentials(self, sub: str):
        """現在のWeb利用者の資格情報を、秘密を公開せずに読み込む。"""
        path = self.user_token_path(sub)
        if not path.exists() or path.stat().st_size == 0:
            raise OAuthConfigurationError("Googleへ再ログインしてください。")
        try:
            from google.oauth2.credentials import Credentials

            credentials = Credentials.from_authorized_user_file(str(path))
        except Exception as exc:  # noqa: BLE001 token内容を例外へ含めない
            raise OAuthConfigurationError("Googleへ再ログインしてください。") from exc
        if not credentials.has_scopes(list(ADDED_SCOPES)):
            raise OAuthConfigurationError(
                "Sheets出力とお知らせ投稿の権限を追加するため、Googleへ再ログインしてください。"
            )
        return credentials

    @staticmethod
    def _safe_return_to(value: str | None) -> str | None:
        if value is None:
            return None
        if re.fullmatch(r"/oauth/mcp/authorize\?request_id=[A-Za-z0-9_-]{20,100}", value):
            return value
        raise OAuthConfigurationError("認証後の戻り先が不正です。")

    def begin(self, return_to: str | None = None) -> str:
        from google_auth_oauthlib.flow import Flow

        os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")
        client_config, _ = self._client_config()
        return_to = self._safe_return_to(return_to)
        state = secrets.token_urlsafe(32)
        flow = Flow.from_client_config(client_config, scopes=OAUTH_SCOPES, state=state)
        flow.redirect_uri = self.redirect_uri
        params: dict[str, Any] = {
            "access_type": "offline",
            "include_granted_scopes": "true",
        }
        # ログイン前はsubが未確定のため、必ずrefresh tokenを要求する。
        params["prompt"] = "consent"
        authorization_url, returned_state = flow.authorization_url(**params)
        with self._lock:
            self._purge_expired_locked()
            self._pending[returned_state] = PendingOAuth(
                created_at=self.clock(),
                code_verifier=flow.code_verifier,
                redirect_uri=self.redirect_uri,
                return_to=return_to,
            )
        return authorization_url

    def return_to(self, state: str) -> str | None:
        with self._lock:
            pending = self._pending.get(state)
        if pending is None or self.clock() - pending.created_at > self.state_ttl:
            raise OAuthStateError("認証状態が一致しません。接続操作を最初からやり直してください。")
        return pending.return_to

    def exchange(
        self, *, state: str, code: str,
        authorize_identity: Callable[[GoogleIdentity], None] | None = None,
    ) -> GoogleIdentity:
        from google_auth_oauthlib.flow import Flow

        os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")
        pending = self.consume_state(state)
        client_config, client_type = self._client_config()
        flow = Flow.from_client_config(
            client_config,
            scopes=OAUTH_SCOPES,
            state=state,
            code_verifier=pending.code_verifier,
            autogenerate_code_verifier=False,
        )
        # 認可URL生成時のPKCE verifierとredirect URIを必ずコード交換へ引き継ぐ。
        # 新しいFlowを作るだけではcode_challengeに対応するverifierが失われる。
        flow.redirect_uri = pending.redirect_uri
        flow.fetch_token(code=code)
        identity = self._verify_identity(
            flow.credentials.id_token,
            audience=self._client_id(client_config, client_type),
        )
        if authorize_identity:
            authorize_identity(identity)
        write_token_atomic(self.user_token_path(identity.sub), flow.credentials.to_json())
        return identity

    @staticmethod
    def _verify_identity(raw_id_token: str | None, *, audience: str) -> GoogleIdentity:
        if not raw_id_token:
            raise OAuthConfigurationError("Googleから本人確認情報を受け取れませんでした。")
        from google.auth.transport.requests import Request
        from google.oauth2 import id_token

        claims = id_token.verify_oauth2_token(raw_id_token, Request(), audience)
        if claims.get("iss") not in {"accounts.google.com", "https://accounts.google.com"}:
            raise OAuthConfigurationError("Google本人確認の発行元が不正です。")
        if claims.get("email_verified") is not True:
            raise OAuthConfigurationError("確認済みメールアドレスを取得できませんでした。")
        sub, email = claims.get("sub"), claims.get("email")
        if not isinstance(sub, str) or not sub or not isinstance(email, str) or not email:
            raise OAuthConfigurationError("Google本人確認情報が不足しています。")
        return GoogleIdentity(sub=sub, email=email.strip().lower())

    def consume_state(self, state: str) -> PendingOAuth:
        if not state:
            raise OAuthStateError("認証状態を確認できません。接続操作を最初からやり直してください。")
        with self._lock:
            pending = self._pending.pop(state, None)
        if pending is None:
            raise OAuthStateError("認証状態が一致しません。接続操作を最初からやり直してください。")
        if self.clock() - pending.created_at > self.state_ttl:
            raise OAuthStateError("認証操作の有効期限が切れました。接続操作を最初からやり直してください。")
        return pending

    def _purge_expired_locked(self) -> None:
        now = self.clock()
        self._pending = {
            state: pending for state, pending in self._pending.items()
            if now - pending.created_at <= self.state_ttl
        }

    def _write_token_atomic(self, value: str) -> None:
        """旧テスト/CLI互換用。Webログインはuser_token_pathへ保存する。"""
        write_token_atomic(self.token_path, value)

    @staticmethod
    def _write_token_compat(path: pathlib.Path, value: str) -> None:
        _write_token_compat(path, value)
