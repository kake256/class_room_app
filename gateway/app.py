"""In-memory cloud relay for the outbound-only 251 agent."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import os
import re
import time
import uuid
from dataclasses import dataclass
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response


_SHA256 = re.compile(r"^[0-9a-fA-F]{64}$")
_SEGMENT = r"[A-Za-z0-9_-]{1,128}"
_ROUTES: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (method, re.compile(pattern)) for method, pattern in [
        ("GET", r"/$"),
        ("GET", r"/ui/$"),
        ("GET", r"/ui/classroom-grader\.user\.js$"),
        ("GET", r"/ui/static/[A-Za-z0-9_.-]{1,128}$"),
        ("GET", r"/oauth2callback$"),
        ("GET", r"/api/v1/(?:model|status|auth/session|courses|courseworks|jobs|audit)$"),
        ("POST", r"/api/v1/auth/(?:google/start|logout)$"),
        ("GET", rf"/api/v1/jobs/{_SEGMENT}$"),
        ("POST", rf"/api/v1/jobs/{_SEGMENT}/cancel$"),
        ("POST", r"/api/v1/jobs$"),
        ("GET", r"/api/v1/mcp/tokens$"),
        ("POST", r"/api/v1/mcp/tokens$"),
        ("DELETE", rf"/api/v1/mcp/tokens/{_SEGMENT}$"),
        ("GET", r"/mcp$"),
        ("POST", r"/mcp$"),
        ("GET", rf"/api/v1/courseworks/{_SEGMENT}/(?:results|report\.csv)$"),
        ("GET", rf"/api/v1/courses/{_SEGMENT}/courseworks$"),
        ("GET", rf"/api/v1/courses/{_SEGMENT}/overview$"),
        ("GET", rf"/api/v1/courses/{_SEGMENT}/courseworks/{_SEGMENT}/(?:settings|readiness|results|report\.csv)$"),
        ("PUT", rf"/api/v1/courses/{_SEGMENT}/courseworks/{_SEGMENT}/settings$"),
        ("PUT", rf"/api/v1/courses/{_SEGMENT}/courseworks/{_SEGMENT}/reviews/{_SEGMENT}$"),
        ("GET", rf"/api/v1/courses/{_SEGMENT}/courseworks/{_SEGMENT}/draft-batches/preview$"),
        ("POST", r"/api/v1/draft-batches(?:/claim)?$"),
        ("GET", r"/api/v1/extension(?:/[A-Za-z0-9_.-]{1,128})*$"),
        ("POST", r"/api/v1/extension(?:/[A-Za-z0-9_.-]{1,128})*$"),
    ]
)
REQUEST_HEADERS = frozenset({
    "cookie", "content-type", "x-csrf-token", "authorization", "accept",
    "mcp-protocol-version", "mcp-session-id", "last-event-id",
})
RESPONSE_HEADERS = frozenset({
    "content-type", "cache-control", "set-cookie", "location", "mcp-session-id",
})


def is_allowed(method: str, path: str) -> bool:
    """The only public-to-local routes. Query strings are checked separately."""
    if path.startswith("/internal/") or ".." in path:
        return False
    return any(method == allowed_method and pattern.fullmatch(path) for allowed_method, pattern in _ROUTES)


def _positive_int(name: str, default: int, maximum: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer") from exc
    if value <= 0 or value > maximum:
        raise RuntimeError(f"{name} is outside its safe range")
    return value


@dataclass(frozen=True)
class Settings:
    agent_token_sha256: str
    request_timeout_seconds: float = 30
    long_poll_seconds: float = 20
    max_request_bytes: int = 262_144
    max_response_bytes: int = 2_097_152
    max_pending: int = 64
    agent_stale_seconds: float = 35
    require_tls: bool = True

    @classmethod
    def from_env(cls) -> "Settings":
        agent_hash = os.environ.get("CGA_GATEWAY_AGENT_TOKEN_SHA256", "")
        if not _SHA256.fullmatch(agent_hash):
            raise RuntimeError("CGA_GATEWAY_AGENT_TOKEN_SHA256 must be a SHA-256 hex digest")
        return cls(
            agent_hash.lower(),
            request_timeout_seconds=_positive_int("CGA_GATEWAY_REQUEST_TIMEOUT_SECONDS", 30, 120),
            long_poll_seconds=_positive_int("CGA_GATEWAY_LONG_POLL_SECONDS", 20, 30),
            max_request_bytes=_positive_int("CGA_GATEWAY_MAX_REQUEST_BYTES", 262_144, 1_048_576),
            max_response_bytes=_positive_int("CGA_GATEWAY_MAX_RESPONSE_BYTES", 2_097_152, 5_242_880),
            max_pending=_positive_int("CGA_GATEWAY_MAX_PENDING", 64, 256),
            agent_stale_seconds=_positive_int("CGA_GATEWAY_AGENT_STALE_SECONDS", 35, 180),
            require_tls=os.environ.get("CGA_GATEWAY_REQUIRE_TLS", "true").lower() not in {"0", "false", "no"},
        )


def token_matches(candidate: str | None, expected_hash: str) -> bool:
    actual = hashlib.sha256((candidate or "").encode()).hexdigest()
    return hmac.compare_digest(actual, expected_hash)


async def read_limited(request: Request, maximum: int) -> bytes:
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > maximum:
            raise HTTPException(status_code=413, detail="request body is too large")
        chunks.append(chunk)
    return b"".join(chunks)


class Broker:
    def __init__(self) -> None:
        self.queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self.last_agent_poll: float | None = None

    def agent_available(self, stale_seconds: float) -> bool:
        return self.last_agent_poll is not None and time.monotonic() - self.last_agent_poll <= stale_seconds

    async def submit(self, envelope: dict[str, Any], timeout: float, maximum: int) -> dict[str, Any]:
        if len(self.pending) >= maximum:
            raise HTTPException(status_code=503, detail="gateway is at capacity")
        request_id = uuid.uuid4().hex
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self.pending[request_id] = future
        envelope["id"] = request_id
        await self.queue.put(envelope)
        try:
            return await asyncio.wait_for(future, timeout=timeout)
        finally:
            self.pending.pop(request_id, None)

    def resolve(self, request_id: str, value: dict[str, Any]) -> bool:
        future = self.pending.get(request_id)
        if future is None or future.done():
            return False
        future.set_result(value)
        return True


def create_app(settings: Settings | None = None) -> FastAPI:
    cfg = settings or Settings.from_env()
    broker = Broker()
    app = FastAPI(title="CGA Outbound Gateway", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.broker = broker

    async def require_agent(x_agent_key: str | None = Header(default=None)) -> None:
        if not token_matches(x_agent_key, cfg.agent_token_sha256):
            raise HTTPException(status_code=401, detail="invalid agent credential")

    @app.middleware("http")
    async def transport_guards(request: Request, call_next: Any) -> Response:
        if cfg.require_tls:
            forwarded = request.headers.get("x-forwarded-proto", "").split(",", 1)[0].strip().lower()
            if request.url.scheme != "https" and forwarded != "https":
                return Response(status_code=400, content="TLS is required")
        length = request.headers.get("content-length")
        ceiling = cfg.max_response_bytes * 2 if request.url.path.startswith("/internal/") else cfg.max_request_bytes
        if length and length.isdigit() and int(length) > ceiling:
            return Response(status_code=413, content="request is too large")
        return await call_next(request)

    @app.get("/health")
    async def health() -> dict[str, object]:
        return {"status": "ok", "agent_connected": broker.agent_available(cfg.agent_stale_seconds)}

    @app.get("/internal/v1/poll", dependencies=[Depends(require_agent)])
    async def poll() -> Response:
        broker.last_agent_poll = time.monotonic()
        try:
            item = await asyncio.wait_for(broker.queue.get(), timeout=cfg.long_poll_seconds)
        except TimeoutError:
            return Response(status_code=204)
        return Response(json.dumps(item, separators=(",", ":")), media_type="application/json")

    @app.post("/internal/v1/responses/{request_id}", dependencies=[Depends(require_agent)])
    async def agent_response(request_id: str, request: Request) -> Response:
        if not re.fullmatch(r"[0-9a-f]{32}", request_id):
            raise HTTPException(status_code=404)
        raw = await read_limited(request, cfg.max_response_bytes * 2)
        try:
            value = json.loads(raw)
            body = base64.b64decode(value["body_b64"], validate=True)
            if not isinstance(value.get("headers", []), list):
                raise ValueError
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise HTTPException(status_code=400, detail="invalid response envelope") from exc
        if len(body) > cfg.max_response_bytes:
            raise HTTPException(status_code=413, detail="local response is too large")
        if not broker.resolve(request_id, value):
            raise HTTPException(status_code=410, detail="request expired")
        return Response(status_code=204)

    @app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE"])
    async def relay(request: Request, path: str) -> Response:
        local_path = "/" + path
        if not is_allowed(request.method, local_path):
            raise HTTPException(status_code=404)
        if not broker.agent_available(cfg.agent_stale_seconds):
            raise HTTPException(status_code=503, detail="local agent is unavailable")
        body = await read_limited(request, cfg.max_request_bytes)
        headers = {name.lower(): value for name, value in request.headers.items() if name.lower() in REQUEST_HEADERS}
        envelope = {
            "method": request.method,
            "path": local_path,
            "query": request.url.query,
            "headers": headers,
            "body_b64": base64.b64encode(body).decode("ascii"),
        }
        try:
            result = await broker.submit(envelope, cfg.request_timeout_seconds, cfg.max_pending)
        except TimeoutError as exc:
            raise HTTPException(status_code=504, detail="local API timed out") from exc
        try:
            response_body = base64.b64decode(result["body_b64"], validate=True)
            status = int(result["status"])
        except (KeyError, TypeError, ValueError) as exc:
            raise HTTPException(status_code=502, detail="invalid local response") from exc
        if len(response_body) > cfg.max_response_bytes or not 200 <= status <= 599:
            raise HTTPException(status_code=502, detail="invalid local response")
        response = Response(content=response_body, status_code=status)
        response.headers["Cache-Control"] = "no-store"
        for pair in result.get("headers", []):
            if isinstance(pair, list) and len(pair) == 2 and str(pair[0]).lower() in RESPONSE_HEADERS:
                if str(pair[0]).lower() == "cache-control":
                    response.headers["Cache-Control"] = str(pair[1])
                else:
                    response.headers.append(str(pair[0]), str(pair[1]))
        return response

    return app
