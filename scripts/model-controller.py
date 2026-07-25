#!/usr/bin/env python3
"""Docker socketを専有するallowlist型モデル切替コントローラー。

APIコンテナへDocker socketを渡さず、固定2プロファイルだけを実行する。
"""
from __future__ import annotations

import hmac
import os
import pathlib
import subprocess
import threading
import json
import urllib.request

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "vllm-server.sh"
ALLOWED = {"q25-7b", "q3-8b", "minicpm-v45"}
EXPECTED = {
    "q25-7b": "Qwen/Qwen2.5-VL-7B-Instruct",
    "q3-8b": "Qwen/Qwen3-VL-8B-Instruct-FP8",
    "minicpm-v45": "openbmb/MiniCPM-V-4_5",
}
SWITCH_LOCK = threading.Lock()
VLLM_BASE_URL = os.environ.get(
    "VLLM_BASE_URL", "http://127.0.0.1:8000"
).rstrip("/")


class SwitchRequest(BaseModel):
    profile: str


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/switch")
def switch(request: SwitchRequest, authorization: str = Header(default="")):
    secret = os.environ.get("CGA_MODEL_CONTROLLER_SECRET", "")
    supplied = authorization.removeprefix("Bearer ")
    if not secret or not hmac.compare_digest(secret, supplied):
        raise HTTPException(status_code=401, detail="unauthorized")
    if request.profile not in ALLOWED:
        raise HTTPException(status_code=400, detail="profile is not allowed")
    if not SWITCH_LOCK.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="model switch already in progress")
    try:
        try:
            with urllib.request.urlopen(f"{VLLM_BASE_URL}/v1/models", timeout=3) as response:
                current = ((json.load(response).get("data") or [{}])[0].get("id"))
        except Exception:  # noqa: BLE001 ローカルvLLM停止中
            current = None
        if current == EXPECTED[request.profile]:
            subprocess.run([str(SCRIPT), "health"], check=True, timeout=150)
            return {"status": "ready", "profile": request.profile, "reused": True}
        subprocess.run([str(SCRIPT), request.profile], check=True, timeout=1500)
    except (OSError, subprocess.SubprocessError) as exc:
        raise HTTPException(status_code=502, detail="model switch failed") from exc
    finally:
        SWITCH_LOCK.release()
    return {"status": "ready", "profile": request.profile, "reused": False}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        app,
        host=os.environ.get("CGA_MODEL_CONTROLLER_HOST", "127.0.0.1"),
        port=8810,
        access_log=False,
    )
