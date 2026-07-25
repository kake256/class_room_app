"""251-side outbound long-poll agent; it opens no inbound listener."""
from __future__ import annotations

import asyncio
import base64
import logging
import os
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx

from .app import REQUEST_HEADERS, RESPONSE_HEADERS, is_allowed


LOG = logging.getLogger("cga-gateway-agent")


@dataclass(frozen=True)
class AgentSettings:
    gateway_url: str
    agent_token: str
    local_url: str
    max_request_bytes: int = 262_144
    max_response_bytes: int = 2_097_152
    local_timeout_seconds: int = 25

    @classmethod
    def from_env(cls) -> "AgentSettings":
        gateway = os.environ.get("CGA_GATEWAY_URL", "").rstrip("/")
        parsed = urlparse(gateway)
        if parsed.scheme != "https" or not parsed.netloc:
            raise RuntimeError("CGA_GATEWAY_URL must be an https URL")
        token = os.environ.get("CGA_GATEWAY_AGENT_TOKEN", "")
        if not token:
            raise RuntimeError("CGA_GATEWAY_AGENT_TOKEN is required")
        return cls(
            gateway, token,
            os.environ.get("CGA_LOCAL_API_URL", "http://127.0.0.1:8800").rstrip("/"),
            int(os.environ.get("CGA_GATEWAY_MAX_REQUEST_BYTES", "262144")),
            int(os.environ.get("CGA_GATEWAY_MAX_RESPONSE_BYTES", "2097152")),
            int(os.environ.get("CGA_GATEWAY_LOCAL_TIMEOUT_SECONDS", "25")),
        )


async def fetch_local(client: httpx.AsyncClient, settings: AgentSettings, job: dict[str, object]) -> dict[str, object]:
    method, path = str(job.get("method", "")), str(job.get("path", ""))
    if not is_allowed(method, path):
        return {"status": 403, "headers": [["Content-Type", "application/json"]], "body_b64": base64.b64encode(b'{"detail":"route denied"}').decode()}
    try:
        body = base64.b64decode(str(job.get("body_b64", "")), validate=True)
        if len(body) > settings.max_request_bytes:
            raise ValueError
        incoming = job.get("headers", {})
        if not isinstance(incoming, dict):
            raise ValueError
        headers = {str(k).lower(): str(v) for k, v in incoming.items() if str(k).lower() in REQUEST_HEADERS}
        query = str(job.get("query", ""))
        if len(query) > 4096 or "#" in query:
            raise ValueError
    except ValueError:
        return {"status": 400, "headers": [["Content-Type", "application/json"]], "body_b64": base64.b64encode(b'{"detail":"invalid relay request"}').decode()}
    try:
        async with client.stream(
            method, settings.local_url + path, params=query, headers=headers, content=body,
            timeout=settings.local_timeout_seconds, follow_redirects=False,
        ) as response:
            chunks: list[bytes] = []
            size = 0
            async for chunk in response.aiter_bytes():
                size += len(chunk)
                if size > settings.max_response_bytes:
                    return {"status": 502, "headers": [["Content-Type", "application/json"]], "body_b64": base64.b64encode(b'{"detail":"local response is too large"}').decode()}
                chunks.append(chunk)
            response_headers = [[name, value] for name, value in response.headers.multi_items() if name.lower() in RESPONSE_HEADERS]
            return {"status": response.status_code, "headers": response_headers, "body_b64": base64.b64encode(b"".join(chunks)).decode("ascii")}
    except httpx.HTTPError:
        return {"status": 502, "headers": [["Content-Type", "application/json"]], "body_b64": base64.b64encode(b'{"detail":"local API unavailable"}').decode()}


async def run(settings: AgentSettings) -> None:
    headers = {"X-Agent-Key": settings.agent_token}
    async with httpx.AsyncClient(headers=headers, timeout=35, follow_redirects=False) as cloud, httpx.AsyncClient() as local:
        while True:
            try:
                response = await cloud.get(settings.gateway_url + "/internal/v1/poll")
                if response.status_code == 204:
                    continue
                response.raise_for_status()
                job = response.json()
                result = await fetch_local(local, settings, job)
                submitted = await cloud.post(settings.gateway_url + f"/internal/v1/responses/{job['id']}", json=result)
                if submitted.status_code not in {204, 410}:
                    submitted.raise_for_status()
            except (httpx.HTTPError, KeyError, ValueError):
                LOG.warning("gateway cycle failed; retrying")
                await asyncio.sleep(2)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    asyncio.run(run(AgentSettings.from_env()))


if __name__ == "__main__":
    main()
