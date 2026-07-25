import asyncio
import base64

import httpx

from gateway.agent import AgentSettings, fetch_local


def test_agent_forwards_allowed_request_and_session_headers():
    async def scenario():
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/api/v1/jobs"
            assert request.method == "POST"
            assert request.headers["cookie"] == "cga_session=signed"
            assert request.headers["x-csrf-token"] == "csrf"
            assert request.content == b'{"phase":"report"}'
            return httpx.Response(201, headers={"Set-Cookie": "cga_session=new; HttpOnly"}, json={"ok": True})

        settings = AgentSettings("https://gateway.example", "agent", "http://127.0.0.1:8800")
        job = {"method": "POST", "path": "/api/v1/jobs", "query": "", "headers": {"cookie": "cga_session=signed", "x-csrf-token": "csrf", "x-bad": "drop"}, "body_b64": base64.b64encode(b'{"phase":"report"}').decode()}
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            result = await fetch_local(client, settings, job)
        assert result["status"] == 201
        assert ["set-cookie", "cga_session=new; HttpOnly"] in result["headers"]
    asyncio.run(scenario())


def test_agent_rejects_unlisted_and_traversal_routes_without_local_request():
    async def scenario():
        settings = AgentSettings("https://gateway.example", "agent", "http://127.0.0.1:8800")
        def must_not_connect(_: httpx.Request) -> httpx.Response:
            raise AssertionError("must not connect")
        async with httpx.AsyncClient(transport=httpx.MockTransport(must_not_connect)) as client:
            for path in ["/api/v1/admin", "/ui/static/../config.yaml", "/internal/v1/poll"]:
                result = await fetch_local(client, settings, {"method": "GET", "path": path})
                assert result["status"] == 403
    asyncio.run(scenario())


def test_agent_relays_stateless_mcp_json_without_logging_token():
    async def scenario():
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/mcp"
            assert request.headers["authorization"] == "Bearer opaque"
            assert request.headers["accept"] == "application/json, text/event-stream"
            return httpx.Response(200, headers={"Content-Type": "application/json"}, json={"ok": True})

        settings = AgentSettings("https://gateway.example", "agent", "http://127.0.0.1:8800")
        job = {"method": "POST", "path": "/mcp", "query": "", "headers": {
            "authorization": "Bearer opaque", "accept": "application/json, text/event-stream",
        }, "body_b64": base64.b64encode(b"{}").decode()}
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            result = await fetch_local(client, settings, job)
        assert result["status"] == 200
    asyncio.run(scenario())
