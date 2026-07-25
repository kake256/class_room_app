"""OAuth 2.1 authorization server provider for URL-only MCP clients."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import pathlib
import re
import secrets
import threading
import time
from typing import Any, Callable
from urllib.parse import parse_qsl, urlencode, urlparse, urlsplit, urlunsplit

from mcp.server.auth.provider import (
    AccessToken, AuthorizationCode, AuthorizationParams, AuthorizeError,
    OAuthAuthorizationServerProvider, RefreshToken, RegistrationError, TokenError,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from pydantic import AnyUrl

from .mcp_tokens import McpTokenStore
from .teacher_review import _atomic_json


SCOPE = "mcp:tools"
PKCE_RE = re.compile(r"[A-Za-z0-9_-]{43,128}")


class McpOAuthProvider(OAuthAuthorizationServerProvider[AuthorizationCode, RefreshToken, AccessToken]):
    def __init__(
        self, path: pathlib.Path, *, public_origin: str, legacy_tokens: McpTokenStore,
        clock: Callable[[], float] = time.time, access_ttl: int = 3600,
        refresh_ttl: int = 30 * 86400, code_ttl: int = 300,
        pending_ttl: int = 600, client_ttl: int = 90 * 86400, max_clients: int = 200,
    ):
        self.path = path
        self.origin = public_origin.rstrip("/")
        self.resource = self.origin + "/mcp"
        self.legacy_tokens = legacy_tokens
        self.clock = clock
        self.access_ttl = max(300, min(int(access_ttl), 86400))
        self.refresh_ttl = max(86400, min(int(refresh_ttl), 90 * 86400))
        self.code_ttl = max(60, min(int(code_ttl), 600))
        self.pending_ttl = max(60, min(int(pending_ttl), 900))
        self.client_ttl = max(86400, min(int(client_ttl), 365 * 86400))
        self.max_clients = max(10, min(int(max_clients), 1000))
        self.lock = threading.RLock()
        self.pending: dict[str, dict[str, Any]] = {}
        self._load()  # fail closed at startup if corrupt

    @staticmethod
    def _hash(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    def _empty(self) -> dict[str, Any]:
        return {"version": 1, "clients": {}, "codes": {}, "access": {},
                "refresh": {}, "used_refresh": {}, "revoked_families": []}

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return self._empty()
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise RuntimeError("MCP OAuth store is corrupt") from exc
        required = {"clients", "codes", "access", "refresh", "used_refresh", "revoked_families"}
        if not isinstance(value, dict) or not required.issubset(value):
            raise RuntimeError("MCP OAuth store is corrupt")
        return value

    def _save(self, value: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.path.parent, 0o700)
        _atomic_json(self.path, value)

    def _prune(self, value: dict[str, Any]) -> None:
        now = int(self.clock())
        value["clients"] = {key: item for key, item in value["clients"].items()
                            if item.get("expires_at", 0) > now}
        for key in ("codes", "access", "refresh", "used_refresh"):
            value[key] = {digest: item for digest, item in value[key].items()
                          if item.get("expires_at", 0) > now}

    @staticmethod
    def _safe_redirect(uri: str) -> bool:
        parsed = urlparse(uri)
        if (parsed.username or parsed.password or parsed.fragment or not parsed.hostname
                or any(key in {"code", "state", "error"} for key, _ in parse_qsl(parsed.query))):
            return False
        if parsed.scheme == "https":
            return True
        return parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}

    @staticmethod
    def _redirect(uri: str, values: dict[str, str]) -> str:
        parsed = urlsplit(uri)
        query = parse_qsl(parsed.query, keep_blank_values=True) + list(values.items())
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), ""))

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        with self.lock:
            value = self._load(); self._prune(value); self._save(value)
            item = value["clients"].get(client_id)
        return OAuthClientInformationFull.model_validate(item["client"]) if item else None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        method = client_info.token_endpoint_auth_method or "client_secret_post"
        if method not in {"none", "client_secret_post", "client_secret_basic"}:
            raise RegistrationError("invalid_client_metadata", "unsupported client authentication method")
        uris = [str(item) for item in (client_info.redirect_uris or [])]
        if not uris or any(not self._safe_redirect(uri) for uri in uris):
            raise RegistrationError("invalid_redirect_uri", "redirect URI is not allowed")
        if not client_info.client_id:
            raise RegistrationError("invalid_client_metadata", "client_id is required")
        client_info.token_endpoint_auth_method = method
        if method == "none":
            client_info.client_secret = None
        elif not client_info.client_secret:
            # RegistrationHandler normally creates this before calling the provider.
            # Keep a safe fallback for direct provider integrations; the same model is
            # returned only by the initial DCR response and later loaded internally.
            client_info.client_secret = secrets.token_urlsafe(32)
        now = int(self.clock())
        with self.lock:
            value = self._load(); self._prune(value)
            if client_info.client_id not in value["clients"] and len(value["clients"]) >= self.max_clients:
                raise RegistrationError("invalid_client_metadata", "client limit reached")
            value["clients"][client_info.client_id] = {
                "client": client_info.model_dump(mode="json"),
                "expires_at": now + self.client_ttl,
            }
            self._save(value)

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        redirect = str(params.redirect_uri)
        registered = {str(item) for item in (client.redirect_uris or [])}
        if redirect not in registered or not self._safe_redirect(redirect):
            raise AuthorizeError("invalid_request", "redirect URI mismatch")
        if not params.redirect_uri_provided_explicitly:
            raise AuthorizeError("invalid_request", "explicit redirect URI required")
        if not PKCE_RE.fullmatch(params.code_challenge):
            raise AuthorizeError("invalid_request", "S256 PKCE is required")
        if params.resource != self.resource:
            raise AuthorizeError("invalid_request", "resource mismatch")
        if set(params.scopes or [SCOPE]) != {SCOPE}:
            raise AuthorizeError("invalid_scope", "mcp:tools is required")
        request_id = secrets.token_urlsafe(32)
        with self.lock:
            self._purge_pending()
            self.pending[self._hash(request_id)] = {
                "created_at": self.clock(), "client_id": client.client_id,
                "client_name": client.client_name or "MCP client", "state": params.state,
                "scopes": params.scopes or [SCOPE], "code_challenge": params.code_challenge,
                "redirect_uri": redirect, "resource": params.resource,
            }
        return self.origin + "/oauth/mcp/authorize?" + urlencode({"request_id": request_id})

    def authorization_request(self, request_id: str, *, owner_ref: str) -> dict[str, Any] | None:
        with self.lock:
            self._purge_pending()
            item = self.pending.get(self._hash(request_id))
            if not item:
                return None
            nonce = secrets.token_urlsafe(24)
            item["nonce_hash"] = self._hash(nonce)
            item["owner_ref"] = owner_ref
            return {"client_name": item["client_name"], "scopes": list(item["scopes"]),
                    "nonce": nonce}

    def complete_authorization(
        self, request_id: str, nonce: str, *, owner_ref: str, role: str, allow: bool,
    ) -> str:
        with self.lock:
            self._purge_pending()
            item = self.pending.pop(self._hash(request_id), None)
        if (not item or item.get("owner_ref") != owner_ref or not nonce
                or not secrets.compare_digest(
                    str(item.get("nonce_hash", "")), self._hash(nonce))):
            raise ValueError("authorization request is invalid")
        query: dict[str, str] = {}
        if item.get("state") is not None:
            query["state"] = item["state"]
        if not allow:
            query["error"] = "access_denied"
            return self._redirect(item["redirect_uri"], query)
        code = secrets.token_urlsafe(32)
        record = {key: item[key] for key in (
            "client_id", "scopes", "code_challenge", "redirect_uri", "resource")}
        record.update(owner_ref=owner_ref, role=role, expires_at=int(self.clock()) + self.code_ttl)
        with self.lock:
            value = self._load(); self._prune(value)
            value["codes"][self._hash(code)] = record; self._save(value)
        query["code"] = code
        return self._redirect(item["redirect_uri"], query)

    def deny_upstream(self, request_id: str) -> str:
        """Consume pending request when Google login is canceled, preventing loops."""
        with self.lock:
            self._purge_pending()
            item = self.pending.pop(self._hash(request_id), None)
        if not item:
            raise ValueError("authorization request is invalid")
        query = {"error": "access_denied"}
        if item.get("state") is not None:
            query["state"] = item["state"]
        return self._redirect(item["redirect_uri"], query)

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str,
    ) -> AuthorizationCode | None:
        with self.lock:
            value = self._load(); self._prune(value)
            item = value["codes"].get(self._hash(authorization_code)); self._save(value)
        if not item or item["client_id"] != client.client_id:
            return None
        return AuthorizationCode(
            code=authorization_code, scopes=item["scopes"], expires_at=item["expires_at"],
            client_id=item["client_id"], code_challenge=item["code_challenge"],
            redirect_uri=AnyUrl(item["redirect_uri"]), redirect_uri_provided_explicitly=True,
            resource=item["resource"], subject=item["owner_ref"])

    def _issue(self, value: dict[str, Any], *, client_id: str, scopes: list[str],
               owner_ref: str, role: str, family: str) -> OAuthToken:
        now = int(self.clock())
        access, refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(40)
        generation = secrets.token_hex(12)
        common = {"client_id": client_id, "scopes": scopes, "owner_ref": owner_ref,
                  "role": role, "family": family, "generation": generation,
                  "resource": self.resource}
        value["access"][self._hash(access)] = {**common, "expires_at": now + self.access_ttl}
        value["refresh"][self._hash(refresh)] = {**common, "expires_at": now + self.refresh_ttl}
        return OAuthToken(access_token=access, expires_in=self.access_ttl,
                          refresh_token=refresh, scope=" ".join(scopes))

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode,
    ) -> OAuthToken:
        digest = self._hash(authorization_code.code)
        with self.lock:
            value = self._load(); self._prune(value); item = value["codes"].pop(digest, None)
            if not item or item["client_id"] != client.client_id:
                raise TokenError("invalid_grant", "authorization code is unavailable")
            token = self._issue(value, client_id=item["client_id"], scopes=item["scopes"],
                                owner_ref=item["owner_ref"], role=item["role"],
                                family=secrets.token_hex(16))
            self._save(value)
        return token

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str,
    ) -> RefreshToken | None:
        with self.lock:
            value = self._load(); self._prune(value)
            item = value["refresh"].get(self._hash(refresh_token)); self._save(value)
        if (not item or item["client_id"] != client.client_id
                or item["family"] in value["revoked_families"]):
            return None
        return RefreshToken(token=refresh_token, client_id=item["client_id"],
                            scopes=item["scopes"], expires_at=item["expires_at"],
                            subject=item["owner_ref"])

    async def exchange_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: RefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        digest = self._hash(refresh_token.token)
        with self.lock:
            value = self._load(); self._prune(value); item = value["refresh"].pop(digest, None)
            if not item or item["client_id"] != client.client_id:
                raise TokenError("invalid_grant", "refresh token is unavailable")
            if item["family"] in value["revoked_families"]:
                raise TokenError("invalid_grant", "token family is revoked")
            requested = scopes or item["scopes"]
            if not set(requested).issubset(item["scopes"]):
                raise TokenError("invalid_scope", "scope escalation is not allowed")
            value["used_refresh"][digest] = {"family": item["family"],
                                              "expires_at": item["expires_at"]}
            value["access"] = {
                key: access for key, access in value["access"].items()
                if not (access.get("family") == item["family"]
                        and access.get("generation") == item.get("generation"))
            }
            token = self._issue(value, client_id=item["client_id"], scopes=requested,
                                owner_ref=item["owner_ref"], role=item["role"],
                                family=item["family"])
            self._save(value)
        return token

    async def load_access_token(self, token: str) -> AccessToken | None:
        with self.lock:
            value = self._load(); self._prune(value)
            item = value["access"].get(self._hash(token)); self._save(value)
        if item and item["family"] not in value["revoked_families"]:
            return AccessToken(token=token, client_id=item["client_id"], scopes=item["scopes"],
                               expires_at=item["expires_at"], resource=item["resource"],
                               subject=item["owner_ref"], claims={
                                   "owner_ref": item["owner_ref"], "role": item["role"]})
        legacy = self.legacy_tokens.verify(token)
        if legacy:
            return AccessToken(token=token, client_id=legacy["owner_ref"], scopes=[SCOPE],
                               expires_at=legacy.get("expires_epoch"), resource=self.resource,
                               subject=legacy["owner_ref"], claims={
                                   "owner_ref": legacy["owner_ref"], "role": legacy["role"]})
        return None

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        raw = token.token
        with self.lock:
            value = self._load(); self._prune(value)
            item = value["access"].get(self._hash(raw)) or value["refresh"].get(self._hash(raw))
            if item and item["family"] not in value["revoked_families"]:
                value["revoked_families"].append(item["family"])
            self._save(value)

    def _purge_pending(self) -> None:
        now = self.clock()
        self.pending = {key: item for key, item in self.pending.items()
                        if now - item["created_at"] <= self.pending_ttl}
