"""採点結果を返すローカルHTTP API(FastAPI)。

用途: ブラウザのユーザースクリプトがこのAPIから点数を取得し、
Classroom成績簿に下書き点を入力する(UI作成課題への書き込み制約を回避)。
学生データを外に出さないため、既定でlocalhostのみにバインドして使う。

エンドポイント:
  GET  /health                     稼働確認
  GET  /grades/{courseWorkId}      report CSV をJSONで返す(要事前 report 実行)
  POST /jobs                       採点ジョブを非同期起動(run/refine/report/full)
  GET  /jobs/{job_id}              ジョブ進捗

認証: config.yaml の api.token を設定すると X-API-Key ヘッダ必須になる。
"""
from __future__ import annotations

import subprocess
import threading
import time
import uuid
from typing import Any, Optional

import pandas as pd
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from .config import Config
from .fetch import normalize_gid

# 成績簿入力に必要な列だけ返す(evidenceはTAの確認用)
REPORT_FIELDS = [
    "student_id", "name", "state", "content_score", "category", "tier",
    "judge_score", "score_after_late", "flags", "evidence",
]

app = FastAPI(title="Classroom Grader API", version="1.0")
# 成績簿ページ(classroom.google.com)のユーザースクリプトからのfetchを許可。
# http://localhost はhttpsページからでもmixed-content対象外なので直接叩ける。
app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://classroom.google.com"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)
_cfg = Config.load()
_jobs: dict[str, dict[str, Any]] = {}
_jobs_lock = threading.Lock()


def require_token(x_api_key: Optional[str] = Header(default=None)) -> None:
    token = _cfg.get("api", "token", default=None)
    if token and x_api_key != token:
        raise HTTPException(status_code=401, detail="invalid or missing X-API-Key")


@app.get("/health")
def health() -> dict[str, Any]:
    return {"status": "ok", "jobs": len(_jobs)}


@app.get("/grades/{coursework_id}")
def get_grades(coursework_id: str, _: None = Depends(require_token)) -> dict[str, Any]:
    cw = normalize_gid(coursework_id)
    csv_path = _cfg.data_dir / "report" / f"{cw}.csv"
    if not csv_path.exists():
        raise HTTPException(
            status_code=404,
            detail=f"{cw} のreportがありません。先に採点(run→refine→report)してください。",
        )
    df = pd.read_csv(csv_path, dtype={"student_id": str})
    cols = [c for c in REPORT_FIELDS if c in df.columns]
    rows = df[cols].where(pd.notna(df[cols]), None).to_dict(orient="records")
    counts = df["category"].value_counts().to_dict() if "category" in df else {}
    return {"coursework_id": cw, "count": len(rows), "category_counts": counts, "grades": rows}


class JobRequest(BaseModel):
    coursework_id: str
    phase: str = "report"          # run | refine | report | full
    lenient: bool = False
    anchor: Optional[str] = None


PHASE_COMMANDS = {
    "run": lambda cw, r: ["run", "--coursework", cw] + (["--lenient"] if r.lenient else []),
    "refine": lambda cw, r: ["refine", "--coursework", cw]
    + (["--anchor", r.anchor] if r.anchor else []),
    "report": lambda cw, r: ["report", "--coursework", cw],
}


def _run_job(job_id: str, cw: str, req: JobRequest) -> None:
    phases = ["run", "refine", "report"] if req.phase == "full" else [req.phase]
    with _jobs_lock:
        _jobs[job_id].update(status="running", phases=phases)
    try:
        for ph in phases:
            args = PHASE_COMMANDS[ph](cw, req)
            proc = subprocess.run(
                ["python", "-m", "grader", *args],
                capture_output=True, text=True, timeout=7200,
            )
            with _jobs_lock:
                _jobs[job_id]["log"] = (proc.stdout + proc.stderr)[-4000:]
            if proc.returncode != 0:
                with _jobs_lock:
                    _jobs[job_id].update(status="failed", failed_phase=ph,
                                         returncode=proc.returncode)
                return
        with _jobs_lock:
            _jobs[job_id].update(status="done", returncode=0)
    except Exception as e:  # noqa: BLE001
        with _jobs_lock:
            _jobs[job_id].update(status="failed", error=str(e))


@app.post("/jobs")
def create_job(req: JobRequest, _: None = Depends(require_token)) -> dict[str, Any]:
    if req.phase not in ("run", "refine", "report", "full"):
        raise HTTPException(status_code=400, detail="phase は run/refine/report/full")
    cw = normalize_gid(req.coursework_id)
    job_id = uuid.uuid4().hex[:12]
    with _jobs_lock:
        _jobs[job_id] = {"id": job_id, "coursework_id": cw, "phase": req.phase,
                         "status": "queued", "created_at": time.strftime("%Y-%m-%dT%H:%M:%S")}
    threading.Thread(target=_run_job, args=(job_id, cw, req), daemon=True).start()
    return {"job_id": job_id, "status": "queued"}


@app.get("/jobs/{job_id}")
def get_job(job_id: str, _: None = Depends(require_token)) -> dict[str, Any]:
    with _jobs_lock:
        job = _jobs.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="job not found")
        return dict(job)
