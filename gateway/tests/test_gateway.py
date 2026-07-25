import asyncio
import base64
import hashlib

import httpx

from gateway.app import Settings, create_app, is_allowed, token_matches


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def app_settings(**changes):
    values = dict(agent_token_sha256=digest("agent"), require_tls=False, long_poll_seconds=0.1, request_timeout_seconds=0.1)
    values.update(changes)
    return Settings(**values)


def test_503_without_connected_agent_and_no_client_token_challenge():
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(app_settings())), base_url="http://test") as client:
            response = await client.get("/api/v1/auth/session")
        assert response.status_code == 503
    asyncio.run(scenario())


def test_long_poll_relays_method_body_limited_headers_and_response_headers():
    async def scenario():
        app = create_app(app_settings())
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            poll_task = asyncio.create_task(client.get("/internal/v1/poll", headers={"X-Agent-Key": "agent"}))
            await asyncio.sleep(0)
            public_task = asyncio.create_task(client.put(
                "/api/v1/courses/course_1/courseworks/work-2/settings?view=full",
                headers={"Cookie": "cga_session=signed", "X-CSRF-Token": "csrf", "X-Not-Allowed": "drop"},
                content=b'{"confirmed":true}',
            ))
            job = (await poll_task).json()
            assert job["method"] == "PUT"
            assert job["path"].endswith("/settings")
            assert job["query"] == "view=full"
            assert base64.b64decode(job["body_b64"]) == b'{"confirmed":true}'
            assert job["headers"]["cookie"] == "cga_session=signed"
            assert "x-not-allowed" not in job["headers"]
            submitted = await client.post(
                f"/internal/v1/responses/{job['id']}", headers={"X-Agent-Key": "agent"},
                json={"status": 200, "headers": [["Content-Type", "application/json"], ["Set-Cookie", "cga_session=new; HttpOnly"]], "body_b64": base64.b64encode(b'{"ok":true}').decode()},
            )
            response = await public_task
        assert submitted.status_code == 204
        assert response.json() == {"ok": True}
        assert "cga_session=new" in response.headers["set-cookie"]
        assert response.headers["cache-control"] == "no-store"
    asyncio.run(scenario())


def test_streamable_http_json_headers_are_relayed():
    async def scenario():
        app = create_app(app_settings())
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            poll_task = asyncio.create_task(client.get(
                "/internal/v1/poll", headers={"X-Agent-Key": "agent"}))
            await asyncio.sleep(0)
            public_task = asyncio.create_task(client.post(
                "/mcp", headers={
                    "Authorization": "Bearer opaque", "Accept": "application/json, text/event-stream",
                    "MCP-Protocol-Version": "2025-06-18",
                }, json={"jsonrpc": "2.0", "id": 1, "method": "initialize"},
            ))
            job = (await poll_task).json()
            assert job["path"] == "/mcp"
            assert job["headers"]["authorization"] == "Bearer opaque"
            assert job["headers"]["mcp-protocol-version"] == "2025-06-18"
            await client.post(
                f"/internal/v1/responses/{job['id']}", headers={"X-Agent-Key": "agent"},
                json={"status": 200, "headers": [["Content-Type", "application/json"]],
                      "body_b64": base64.b64encode(b'{"jsonrpc":"2.0","id":1,"result":{}}').decode()},
            )
            response = await public_task
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("application/json")
    asyncio.run(scenario())


def test_timeout_capacity_auth_size_and_tls_guards():
    async def scenario():
        app = create_app(app_settings(require_tls=True, max_request_bytes=8))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            assert (await client.get("/health")).status_code == 400
            assert (await client.get("/internal/v1/poll", headers={"X-Forwarded-Proto": "https", "X-Agent-Key": "wrong"})).status_code == 401
            too_big = await client.post("/api/v1/jobs", headers={"X-Forwarded-Proto": "https", "Content-Length": "9"}, content=b"123456789")
        assert too_big.status_code == 413
    asyncio.run(scenario())
    assert token_matches("agent", digest("agent"))
    assert not token_matches("wrong", digest("agent"))


def test_connected_agent_that_does_not_answer_returns_504():
    async def scenario():
        app = create_app(app_settings(request_timeout_seconds=0.01))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            poll_task = asyncio.create_task(client.get("/internal/v1/poll", headers={"X-Agent-Key": "agent"}))
            await asyncio.sleep(0)
            public_task = asyncio.create_task(client.get("/api/v1/auth/session"))
            assert (await poll_task).status_code == 200
            assert (await public_task).status_code == 504
    asyncio.run(scenario())


def test_allowlist_denies_internal_arbitrary_and_wrong_methods():
    assert is_allowed("GET", "/ui/static/app.js")
    assert is_allowed("POST", "/api/v1/jobs")
    assert is_allowed("PUT", "/api/v1/courses/c/courseworks/w/reviews/s")
    assert is_allowed("POST", "/mcp")
    assert is_allowed("GET", "/mcp")
    assert is_allowed("POST", "/api/v1/mcp/tokens")
    assert is_allowed("GET", "/api/v1/courses/123/overview")
    assert is_allowed("DELETE", "/api/v1/mcp/tokens/0123456789abcdef")
    assert not is_allowed("PUT", "/mcp")
    assert not is_allowed("GET", "/internal/v1/poll")
    assert not is_allowed("GET", "/api/v1/audit/export")
    assert not is_allowed("POST", "/api/v1/courses")
    assert not is_allowed("GET", "/ui/static/../secret")
