"""Web UI向けGoogle OAuth処理。Googleへの外部通信はすべて偽装する。"""
import errno
import json
import stat
from urllib.parse import parse_qs, urlparse

import pytest

from grader.config import Config
from grader.fetch import SCOPES
from grader.google_auth import (
    GoogleOAuthManager, OAuthConfigurationError, OAuthStateError,
    OAUTH_SCOPES, SHEETS_SCOPE,
)


class FakeCredentials:
    id_token = "signed-test-id-token"

    def to_json(self):
        return json.dumps({"token": "test-access", "refresh_token": "test-refresh"})


def test_sheets_scope_is_web_only():
    assert SHEETS_SCOPE in OAUTH_SCOPES
    assert SHEETS_SCOPE not in SCOPES


class FakeFlow:
    def __init__(self, state, code_verifier=None):
        self.state = state
        self.redirect_uri = None
        self.code_verifier = code_verifier or f"verifier-{state}"
        self.credentials = FakeCredentials()
        self.fetched_code = None

    def authorization_url(self, **kwargs):
        assert kwargs["access_type"] == "offline"
        assert kwargs["include_granted_scopes"] == "true"
        assert kwargs["prompt"] == "consent"
        return f"https://accounts.example/authorize?state={self.state}", self.state

    def fetch_token(self, *, code):
        self.fetched_code = code


@pytest.fixture
def manager(tmp_path, monkeypatch):
    credentials = tmp_path / "credentials.json"
    credentials.write_text(json.dumps({"installed": {"client_id": "test", "client_secret": "test"}}))
    cfg = Config({"classroom": {
        "credentials_file": str(credentials),
        "token_file": str(tmp_path / "token.json"),
        "user_token_dir": str(tmp_path / "oauth_tokens"),
        "oauth_redirect_uri": "http://localhost:8800/oauth2callback",
        "oauth_state_ttl_seconds": 60,
    }})
    now = [1000.0]
    mgr = GoogleOAuthManager(cfg, clock=lambda: now[0])
    made = []

    def fake_from_client_config(_config, scopes, state, code_verifier=None,
                                autogenerate_code_verifier=True):
        assert "openid" in scopes
        assert "https://www.googleapis.com/auth/userinfo.email" in scopes
        if code_verifier is not None:
            assert autogenerate_code_verifier is False
        flow = FakeFlow(state, code_verifier)
        made.append(flow)
        return flow

    import google_auth_oauthlib.flow
    import google.oauth2.id_token
    monkeypatch.setattr(google_auth_oauthlib.flow.Flow, "from_client_config", fake_from_client_config)
    monkeypatch.setattr(
        google.oauth2.id_token, "verify_oauth2_token",
        lambda token, _request, audience: {
            "iss": "https://accounts.google.com", "sub": "verified-sub",
            "email": "Teacher@Example.edu", "email_verified": True,
            "aud": audience,
        } if token == "signed-test-id-token" and audience == "test" else {},
    )
    return mgr, now, made


def test_begin_and_successful_exchange_saves_private_token_atomically(manager):
    mgr, _now, made = manager
    url = mgr.begin()
    state = parse_qs(urlparse(url).query)["state"][0]
    assert made[0].redirect_uri == "http://localhost:8800/oauth2callback"

    identity = mgr.exchange(state=state, code="one-time-code")

    token = mgr.user_token_path(identity.sub)
    assert json.loads(token.read_text())["refresh_token"] == "test-refresh"
    assert stat.S_IMODE(token.stat().st_mode) == 0o600
    assert made[-1].fetched_code == "one-time-code"
    assert made[-1].code_verifier == made[0].code_verifier
    assert made[-1].redirect_uri == made[0].redirect_uri
    assert identity.sub == "verified-sub" and identity.email == "teacher@example.edu"
    assert not mgr.token_path.exists()  # Web OAuthはCLI用共有tokenを上書きしない
    with pytest.raises(OAuthStateError, match="一致しません"):
        mgr.exchange(state=state, code="replay-code")


def test_begin_only_accepts_safe_mcp_relative_return(manager):
    mgr, _now, _made = manager
    safe = "/oauth/mcp/authorize?request_id=" + "a" * 24
    url = mgr.begin(return_to=safe)
    state = parse_qs(urlparse(url).query)["state"][0]
    assert mgr.return_to(state) == safe
    with pytest.raises(OAuthConfigurationError):
        mgr.begin(return_to="//evil.example/oauth/mcp/authorize?request_id=" + "a" * 24)
    with pytest.raises(OAuthConfigurationError):
        mgr.begin(return_to=safe + "&next=x")


def test_status_exposes_no_secrets(manager):
    mgr, _now, _made = manager
    status = mgr.status()
    assert status["credentials_present"] is True
    assert status["credentials_valid"] is True
    assert status["client_type"] == "installed"
    assert status["token_present"] is False
    assert status["connected"] is False
    assert status["needs_reconnect"] is True
    assert "client_secret" not in status and "token" not in status


def test_existing_web_token_without_sheets_scope_requires_reconnect(manager):
    mgr, _now, _made = manager
    from google.oauth2.credentials import Credentials

    credentials = Credentials(
        token="access", refresh_token="refresh", token_uri="https://oauth2.googleapis.com/token",
        client_id="test", client_secret="test", scopes=SCOPES,
    )
    path = mgr.user_token_path("legacy-user")
    path.parent.mkdir(parents=True)
    path.write_text(credentials.to_json())
    status = mgr.status("legacy-user")
    assert status["connected"] is True
    assert status["sheets_scope_granted"] is False
    assert status["needs_reconnect"] is True
    with pytest.raises(OAuthConfigurationError, match="再ログイン"):
        mgr.user_credentials("legacy-user")


def test_user_tokens_are_anonymous_and_separated(manager, monkeypatch):
    mgr, _now, _made = manager
    identities = iter([
        {"iss": "accounts.google.com", "sub": "teacher-one", "email": "one@example.edu",
         "email_verified": True},
        {"iss": "accounts.google.com", "sub": "teacher-two", "email": "two@example.edu",
         "email_verified": True},
    ])
    import google.oauth2.id_token
    monkeypatch.setattr(google.oauth2.id_token, "verify_oauth2_token",
                        lambda *_args: next(identities))
    first = mgr.exchange(state=parse_qs(urlparse(mgr.begin()).query)["state"][0], code="one")
    second = mgr.exchange(state=parse_qs(urlparse(mgr.begin()).query)["state"][0], code="two")
    assert mgr.user_token_path(first.sub) != mgr.user_token_path(second.sub)
    assert first.sub not in mgr.user_token_path(first.sub).name
    assert mgr.user_token_path(first.sub).exists() and mgr.user_token_path(second.sub).exists()
    assert stat.S_IMODE(mgr.user_token_path(first.sub).stat().st_mode) == 0o600


def test_identity_authorization_happens_before_token_save(manager):
    mgr, _now, _made = manager
    state = parse_qs(urlparse(mgr.begin()).query)["state"][0]

    def reject(_identity):
        raise PermissionError("not allowed")

    with pytest.raises(PermissionError):
        mgr.exchange(state=state, code="code", authorize_identity=reject)
    assert not mgr.token_path.exists()


def test_unverified_google_email_is_rejected(manager, monkeypatch):
    mgr, _now, _made = manager
    state = parse_qs(urlparse(mgr.begin()).query)["state"][0]
    import google.oauth2.id_token
    monkeypatch.setattr(
        google.oauth2.id_token, "verify_oauth2_token",
        lambda *_args: {"iss": "accounts.google.com", "sub": "sub",
                       "email": "user@example.edu", "email_verified": False},
    )
    with pytest.raises(OAuthConfigurationError, match="確認済みメール"):
        mgr.exchange(state=state, code="code")
    assert not mgr.token_path.exists()


def test_state_mismatch_is_rejected(manager):
    mgr, _now, _made = manager
    mgr.begin()
    with pytest.raises(OAuthStateError, match="一致しません"):
        mgr.exchange(state="wrong", code="code")
    assert not mgr.token_path.exists()


def test_expired_state_is_rejected(manager):
    mgr, now, _made = manager
    url = mgr.begin()
    state = parse_qs(urlparse(url).query)["state"][0]
    now[0] += 61
    with pytest.raises(OAuthStateError, match="有効期限"):
        mgr.exchange(state=state, code="code")
    assert not mgr.token_path.exists()


def test_bind_mount_fallback_keeps_private_mode(manager, monkeypatch):
    mgr, _now, _made = manager
    mgr.token_path.touch()

    def busy(_source, _target):
        raise OSError(errno.EBUSY, "bind mount")

    monkeypatch.setattr("grader.google_auth.os.replace", busy)
    mgr._write_token_atomic('{"token":"fallback"}')
    assert json.loads(mgr.token_path.read_text())["token"] == "fallback"
    assert stat.S_IMODE(mgr.token_path.stat().st_mode) == 0o600


def test_read_only_parent_mkstemp_fallback_writes_existing_token(manager, monkeypatch):
    mgr, _now, _made = manager
    mgr.token_path.write_text("old")

    def denied(*_args, **_kwargs):
        raise OSError(errno.EACCES, "read-only parent")

    monkeypatch.setattr("grader.google_auth.tempfile.mkstemp", denied)
    mgr._write_token_atomic('{"token":"existing-bind-mount"}')
    assert json.loads(mgr.token_path.read_text())["token"] == "existing-bind-mount"
    assert stat.S_IMODE(mgr.token_path.stat().st_mode) == 0o600


def test_read_only_parent_without_writable_token_fails_clearly(manager, monkeypatch):
    mgr, _now, _made = manager

    def denied(*_args, **_kwargs):
        raise OSError(errno.EACCES, "permission denied")

    monkeypatch.setattr("grader.google_auth.tempfile.mkstemp", denied)
    monkeypatch.setattr("grader.google_auth.os.open", denied)
    with pytest.raises(OAuthConfigurationError, match="トークン保存先の書込権限"):
        mgr._write_token_atomic("value")
