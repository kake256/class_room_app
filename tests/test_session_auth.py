import pytest

from grader.config import Config
from grader.session_auth import InvalidSession, SessionManager, UserNotAllowed


def make_manager(monkeypatch, now, **web_auth):
    monkeypatch.setenv("CGA_SESSION_SECRET", "test-session-secret-with-enough-entropy")
    return SessionManager(Config({"web_auth": web_auth}), clock=lambda: now[0])


def test_session_signature_expiry_and_tampering(monkeypatch):
    now = [1000]
    manager = make_manager(monkeypatch, now, session_ttl_seconds=60)
    token, created = manager.create(sub="google-sub", email="Teacher@Example.edu")
    verified = manager.verify(token)
    assert verified.sub == "google-sub"
    assert verified.email == "teacher@example.edu"
    assert verified.csrf_token == created.csrf_token

    body, signature = token.split(".")
    with pytest.raises(InvalidSession):
        manager.verify(f"{body}x.{signature}")
    now[0] += 60
    with pytest.raises(InvalidSession, match="有効期限"):
        manager.verify(token)


def test_allowlist_normalizes_email_and_domain(monkeypatch):
    now = [1000]
    manager = make_manager(
        monkeypatch, now,
        allowed_emails=["Specific@Other.example"],
        allowed_domains=["@Example.EDU"],
    )
    manager.authorize("TEACHER@example.edu")
    manager.authorize("specific@other.example")
    with pytest.raises(UserNotAllowed):
        manager.authorize("outsider@example.net")


def test_roles_resolve_by_priority_and_are_in_session(monkeypatch):
    manager = make_manager(
        monkeypatch, [1000],
        roles={
            "admin": {"emails": ["Boss@Example.edu"]},
            "grader": {"domains": ["@example.edu"]},
            "viewer": {"emails": ["boss@example.edu"], "domains": ["read.example"]},
        },
    )
    assert manager.resolve_role("BOSS@example.edu") == "admin"
    assert manager.resolve_role("teacher@example.edu") == "grader"
    assert manager.resolve_role("guest@read.example") == "viewer"
    with pytest.raises(UserNotAllowed):
        manager.resolve_role("outsider@example.net")
    token, created = manager.create(sub="sub", email="boss@example.edu")
    assert created.role == "admin"
    assert manager.verify(token).role == "admin"


def test_empty_roles_keep_allowlist_grader_compatibility(monkeypatch):
    manager = make_manager(
        monkeypatch, [1000], allowed_domains=["example.edu"], roles={}
    )
    assert manager.resolve_role("teacher@example.edu") == "grader"
    with pytest.raises(UserNotAllowed):
        manager.resolve_role("outsider@example.net")


def test_unconfigured_secret_is_persistent_and_mode_600(monkeypatch, tmp_path):
    monkeypatch.delenv("CGA_SESSION_SECRET", raising=False)
    cfg = Config({"paths": {"data_dir": str(tmp_path)}})
    first = SessionManager(cfg)
    token, _ = first.create(sub="sub", email="teacher@example.edu")
    second = SessionManager(cfg)
    assert second.verify(token).role == "grader"
    assert (tmp_path / "session_secret").stat().st_mode & 0o777 == 0o600


def test_short_configured_secret_is_rejected(monkeypatch):
    monkeypatch.setenv("CGA_SESSION_SECRET", "short")
    with pytest.raises(ValueError, match="32"):
        SessionManager(Config({}))
    monkeypatch.delenv("CGA_SESSION_SECRET")
    with pytest.raises(ValueError, match="32"):
        SessionManager(Config({"web_auth": {"session_secret": "short-config"}}))


def test_fail_closed_requires_explicit_roles(monkeypatch):
    monkeypatch.setenv("CGA_SESSION_SECRET", "x" * 32)
    manager = SessionManager(Config({"web_auth": {"require_explicit_roles": True}}))
    with pytest.raises(UserNotAllowed, match="明示的"):
        manager.authorize("teacher@example.edu")
