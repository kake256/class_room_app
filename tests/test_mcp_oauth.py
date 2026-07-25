import asyncio
import base64
import hashlib
import json
import os
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient
from mcp.server.auth.provider import AuthorizationParams
from mcp.shared.auth import OAuthClientInformationFull
from pydantic import AnyUrl

from grader.mcp_oauth import McpOAuthProvider
from grader.mcp_tokens import McpTokenStore
from grader.mcp_server import McpServices, build_mcp


def client(client_id="client-1", redirect="http://127.0.0.1:7777/callback"):
    return OAuthClientInformationFull(
        client_id=client_id, client_name="Claude <unsafe>",
        redirect_uris=[AnyUrl(redirect)], token_endpoint_auth_method="none",
        scope="mcp:tools")


def challenge(verifier):
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")


def test_provider_code_tokens_rotation_revoke_hash_only_and_modes(tmp_path):
    async def scenario():
        now = [1_700_000_000]
        legacy = McpTokenStore(tmp_path / "legacy.json", clock=lambda: now[0])
        provider = McpOAuthProvider(
            tmp_path / "oauth" / "store.json", public_origin="https://grader.example",
            legacy_tokens=legacy, clock=lambda: now[0])
        registered = client()
        await provider.register_client(registered)
        assert (await provider.get_client("client-1")).client_name == "Claude <unsafe>"
        verifier = "v" * 43
        consent = await provider.authorize(registered, AuthorizationParams(
            state="state-kept", scopes=["mcp:tools"], code_challenge=challenge(verifier),
            redirect_uri=AnyUrl("http://127.0.0.1:7777/callback"),
            redirect_uri_provided_explicitly=True,
            resource="https://grader.example/mcp"))
        request_id = parse_qs(urlparse(consent).query)["request_id"][0]
        pending = provider.authorization_request(request_id, owner_ref="a" * 64)
        redirect = provider.complete_authorization(
            request_id, pending["nonce"], owner_ref="a" * 64, role="grader", allow=True)
        query = parse_qs(urlparse(redirect).query)
        assert query["state"] == ["state-kept"]
        raw_code = query["code"][0]
        loaded = await provider.load_authorization_code(registered, raw_code)
        assert loaded.resource == "https://grader.example/mcp"
        tokens = await provider.exchange_authorization_code(registered, loaded)
        assert await provider.load_authorization_code(registered, raw_code) is None
        access = await provider.load_access_token(tokens.access_token)
        assert access.resource == "https://grader.example/mcp"
        assert access.claims == {"owner_ref": "a" * 64, "role": "grader"}
        refresh = await provider.load_refresh_token(registered, tokens.refresh_token)
        rotated = await provider.exchange_refresh_token(registered, refresh, ["mcp:tools"])
        assert await provider.load_refresh_token(registered, tokens.refresh_token) is None
        assert await provider.load_access_token(tokens.access_token) is None
        new_access = await provider.load_access_token(rotated.access_token)
        await provider.revoke_token(new_access)
        assert await provider.load_access_token(rotated.access_token) is None
        persisted = (tmp_path / "oauth" / "store.json").read_text()
        for raw in (raw_code, tokens.access_token, tokens.refresh_token,
                    rotated.access_token, rotated.refresh_token):
            assert raw not in persisted
        assert oct(os.stat(tmp_path / "oauth").st_mode & 0o777) == "0o700"
        assert oct(os.stat(tmp_path / "oauth" / "store.json").st_mode & 0o777) == "0o600"
        legacy_token = legacy.create("b" * 64, "grader", 1)["token"]
        legacy_access = await provider.load_access_token(legacy_token)
        assert legacy_access.claims["owner_ref"] == "b" * 64
    asyncio.run(scenario())


def test_redirect_resource_owner_nonce_expiry_and_corrupt_fail_closed(tmp_path):
    async def scenario():
        now = [100.0]
        provider = McpOAuthProvider(
            tmp_path / "store.json", public_origin="https://grader.example",
            legacy_tokens=McpTokenStore(tmp_path / "legacy.json"), clock=lambda: now[0],
            pending_ttl=60)
        with pytest.raises(Exception):
            await provider.register_client(client(redirect="https://good.example/cb#fragment"))
        registered = client(); await provider.register_client(registered)
        query_client = client("query-client", "http://127.0.0.1:7777/callback?existing=1")
        await provider.register_client(query_client)
        params = AuthorizationParams(
            state=None, scopes=["mcp:tools"], code_challenge="x" * 43,
            redirect_uri=AnyUrl("http://127.0.0.1:7777/callback"),
            redirect_uri_provided_explicitly=True, resource="https://wrong.example/mcp")
        with pytest.raises(Exception):
            await provider.authorize(registered, params)
        params.resource = "https://grader.example/mcp"
        consent = await provider.authorize(registered, params)
        rid = parse_qs(urlparse(consent).query)["request_id"][0]
        pending = provider.authorization_request(rid, owner_ref="a" * 64)
        with pytest.raises(ValueError):
            provider.complete_authorization(
                rid, pending["nonce"], owner_ref="b" * 64, role="grader", allow=True)
        # owner mismatch consumes the one-time request; a fresh request expires safely.
        consent = await provider.authorize(registered, params)
        rid = parse_qs(urlparse(consent).query)["request_id"][0]
        now[0] += 61
        assert provider.authorization_request(rid, owner_ref="a" * 64) is None
        query_params = AuthorizationParams(
            state="s", scopes=["mcp:tools"], code_challenge="x" * 43,
            redirect_uri=AnyUrl("http://127.0.0.1:7777/callback?existing=1"),
            redirect_uri_provided_explicitly=True, resource="https://grader.example/mcp")
        consent = await provider.authorize(query_client, query_params)
        rid = parse_qs(urlparse(consent).query)["request_id"][0]
        item = provider.authorization_request(rid, owner_ref="a" * 64)
        redirect = provider.complete_authorization(
            rid, item["nonce"], owner_ref="a" * 64, role="grader", allow=False)
        assert parse_qs(urlparse(redirect).query) == {
            "existing": ["1"], "state": ["s"], "error": ["access_denied"]}
    asyncio.run(scenario())
    broken = tmp_path / "broken.json"; broken.write_text("not-json")
    with pytest.raises(RuntimeError):
        McpOAuthProvider(broken, public_origin="https://grader.example",
                         legacy_tokens=McpTokenStore(tmp_path / "legacy2.json"))
    assert broken.read_text() == "not-json"


def test_sdk_http_metadata_dcr_authorize_s256_token_and_401(tmp_path):
    legacy = McpTokenStore(tmp_path / "legacy.json")
    provider = McpOAuthProvider(
        tmp_path / "oauth.json", public_origin="https://grader.example",
        legacy_tokens=legacy)
    callbacks = {name: (lambda *_args, **_kwargs: {"ok": True})
                 for name in McpServices.__dataclass_fields__}
    _server, app = build_mcp(
        legacy, McpServices(**callbacks), public_url="https://grader.example",
        oauth_provider=provider)
    verifier = "z" * 43
    with TestClient(app, base_url="https://grader.example") as client_http:
        metadata = client_http.get("/.well-known/oauth-authorization-server")
        assert metadata.status_code == 200
        assert metadata.json()["registration_endpoint"] == "https://grader.example/register"
        registered = client_http.post("/register", json={
            "client_name": "Claude", "redirect_uris": ["http://127.0.0.1:8765/cb"],
            "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"], "scope": "mcp:tools",
        })
        assert registered.status_code == 201
        client_id = registered.json()["client_id"]
        authorization = client_http.get("/authorize", params={
            "response_type": "code", "client_id": client_id,
            "redirect_uri": "http://127.0.0.1:8765/cb", "scope": "mcp:tools",
            "state": "kept", "code_challenge": challenge(verifier),
            "code_challenge_method": "S256", "resource": "https://grader.example/mcp",
        }, follow_redirects=False)
        assert authorization.status_code in {302, 303, 307}
        consent = authorization.headers["location"]
        request_id = parse_qs(urlparse(consent).query)["request_id"][0]
        pending = provider.authorization_request(request_id, owner_ref="a" * 64)
        callback = provider.complete_authorization(
            request_id, pending["nonce"], owner_ref="a" * 64,
            role="grader", allow=True)
        code = parse_qs(urlparse(callback).query)["code"][0]
        wrong = client_http.post("/token", data={
            "grant_type": "authorization_code", "client_id": client_id,
            "code": code, "redirect_uri": "http://127.0.0.1:8765/cb",
            "code_verifier": "wrong" * 11, "resource": "https://grader.example/mcp",
        })
        assert wrong.status_code == 400
        token = client_http.post("/token", data={
            "grant_type": "authorization_code", "client_id": client_id,
            "code": code, "redirect_uri": "http://127.0.0.1:8765/cb",
            "code_verifier": verifier, "resource": "https://grader.example/mcp",
        })
        assert token.status_code == 200 and token.json()["refresh_token"]

        # Claude Desktop may omit token_endpoint_auth_method. The SDK defaults
        # that DCR request to a confidential client and returns its secret once.
        confidential = client_http.post("/register", json={
            "client_name": "Claude Desktop",
            "redirect_uris": ["http://127.0.0.1:8766/callback"],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"], "scope": "mcp:tools",
        })
        assert confidential.status_code == 201
        confidential_body = confidential.json()
        assert confidential_body["token_endpoint_auth_method"] == "client_secret_post"
        assert confidential_body["client_secret"]
        assert confidential_body["client_secret"] in (tmp_path / "oauth.json").read_text()
        assert oct((tmp_path / "oauth.json").stat().st_mode & 0o777) == "0o600"
        confidential_verifier = "q" * 43
        confidential_auth = client_http.get("/authorize", params={
            "response_type": "code", "client_id": confidential_body["client_id"],
            "redirect_uri": "http://127.0.0.1:8766/callback", "scope": "mcp:tools",
            "state": "confidential-state",
            "code_challenge": challenge(confidential_verifier),
            "code_challenge_method": "S256", "resource": "https://grader.example/mcp",
        }, follow_redirects=False)
        assert confidential_auth.status_code in {302, 303, 307}
        confidential_request_id = parse_qs(
            urlparse(confidential_auth.headers["location"]).query)["request_id"][0]
        confidential_pending = provider.authorization_request(
            confidential_request_id, owner_ref="b" * 64)
        confidential_callback = provider.complete_authorization(
            confidential_request_id, confidential_pending["nonce"],
            owner_ref="b" * 64, role="grader", allow=True)
        confidential_code = parse_qs(urlparse(confidential_callback).query)["code"][0]
        confidential_token = client_http.post("/token", data={
            "grant_type": "authorization_code",
            "client_id": confidential_body["client_id"],
            "client_secret": confidential_body["client_secret"],
            "code": confidential_code,
            "redirect_uri": "http://127.0.0.1:8766/callback",
            "code_verifier": confidential_verifier,
            "resource": "https://grader.example/mcp",
        })
        assert confidential_token.status_code == 200
        assert confidential_token.json()["access_token"]
        unauthenticated = client_http.get("/mcp")
        assert unauthenticated.status_code == 401
        assert "resource_metadata=" in unauthenticated.headers["www-authenticate"]
        bad_redirect = client_http.post("/register", json={
            "client_name": "Bad", "redirect_uris": ["https://good.example/cb#fragment"],
            "token_endpoint_auth_method": "none",
        })
        assert bad_redirect.status_code == 400
