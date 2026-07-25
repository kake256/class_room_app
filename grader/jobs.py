"""採点ジョブの永続化、排他制御、実行管理。"""
from __future__ import annotations

import json
import pathlib
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable


TERMINAL_STATUSES = {"succeeded", "failed", "interrupted", "canceled", "blocked"}
VALID_PHASES = {"prepare", "run", "refine", "report", "full"}


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


# grade_hybridが返すtimestampsのうち、ジョブへ永続化して進捗APIへ出す項目。
GRADING_TIMESTAMP_KEYS = (
    "first_primary_saved_at", "primary_completed_at", "review_started_at",
    "first_ready_for_human_review_at", "review_completed_at",
)


def _last_json_line(stdout: str) -> dict[str, Any] | None:
    for line in reversed((stdout or "").strip().splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            value = json.loads(line)
        except ValueError:
            continue
        return value if isinstance(value, dict) else None
    return None


def _request_timing_summary(stdout: str) -> dict[str, Any]:
    """採点出力からリクエスト単位計測の要約だけを取り出す(p50/p95/初回ready)。"""
    value = _last_json_line(stdout) or {}
    summary = value.get("request_timing_summary")
    return summary if isinstance(summary, dict) else {}


def _grading_timestamps(stdout: str) -> dict[str, str]:
    """hybrid採点コマンドのJSON出力からtimestampsだけを取り出す。

    採点結果JSONは最終行に出力される。解析できない場合は計測を諦めるだけで、
    ジョブ自体は成功として扱う(計測は補助情報)。
    """
    for line in reversed((stdout or "").strip().splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            value = json.loads(line)
        except ValueError:
            continue
        stamps = value.get("timestamps") if isinstance(value, dict) else None
        if not isinstance(stamps, dict):
            return {}
        return {key: str(stamps[key]) for key in GRADING_TIMESTAMP_KEYS
                if isinstance(stamps.get(key), str)}
    return {}


@dataclass
class JobOptions:
    lenient: bool = False
    force: bool = False
    anchor: str | None = None
    # 採点姿勢: auto=課題タイプの既定(感想文=甘め/演習=厳しめ) / lenient / strict。
    # lenient(bool)は旧UI互換のため残す(True時はstance=lenient扱い)
    stance: str = "auto"
    allow_partial: bool = False
    allow_stale_meta: bool = False
    # 確認済み採点基準を使うfullジョブは、高速なtext batch + visual fallbackで実行する。
    hybrid: bool = False


class JobConflictError(RuntimeError):
    pass


class JobStore:
    """data/jobsにジョブを原子的に保存する。"""

    def __init__(self, root: pathlib.Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.recover_interrupted()

    def _path(self, job_id: str) -> pathlib.Path:
        return self.root / f"{job_id}.json"

    def save(self, job: dict[str, Any]) -> None:
        with self.lock:
            path = self._path(job["id"])
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(job, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(path)

    def get(self, job_id: str) -> dict[str, Any] | None:
        path = self._path(job_id)
        if not path.exists():
            return None
        with self.lock:
            return json.loads(path.read_text(encoding="utf-8"))

    def list(self) -> list[dict[str, Any]]:
        jobs = []
        with self.lock:
            for path in self.root.glob("*.json"):
                try:
                    jobs.append(json.loads(path.read_text(encoding="utf-8")))
                except (OSError, json.JSONDecodeError):
                    continue
        return sorted(jobs, key=lambda j: (j.get("created_ns", 0), j.get("id", "")),
                      reverse=True)

    def update(self, job_id: str, **changes: Any) -> dict[str, Any]:
        with self.lock:
            job = self.get(job_id)
            if job is None:
                raise KeyError(job_id)
            job.update(changes)
            self.save(job)
            return job

    def recover_interrupted(self) -> None:
        for job in self.list():
            if job.get("status") in {"running", "queued"}:
                job.update(status="interrupted", finished_at=_now(),
                           error="API再起動により安全に再開できないため中断しました")
                self.save(job)

    def active(self) -> list[dict[str, Any]]:
        return [j for j in self.list() if j.get("status") in {"queued", "running"}]


class JobService:
    def __init__(
        self,
        store: JobStore,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        token_resolver: Callable[[str], pathlib.Path] | None = None,
        model_guard: Callable[[str], tuple[bool, str]] | None = None,
        settings_guard: Callable[[str, str, str | None], bool] | None = None,
        model_switcher: Callable[[str], tuple[bool, str]] | None = None,
    ):
        self.store = store
        self.runner = runner
        self.token_resolver = token_resolver
        self.model_guard = model_guard
        self.settings_guard = settings_guard
        self.model_switcher = model_switcher
        self.lock = threading.Lock()
        self._worker_running = False

    def create(
        self, coursework_id: str, phase: str, options: JobOptions, *,
        course_id: str | None = None, token_ref: str | None = None,
        settings_revision: str | None = None,
    ) -> dict[str, Any]:
        if phase not in VALID_PHASES:
            raise ValueError("phase は prepare/run/refine/report/full のいずれかです")
        start_worker = False
        with self.lock:
            if any(
                item.get("token_ref") == token_ref
                and item.get("course_id") == course_id
                and item.get("coursework_id") == coursework_id
                for item in self.store.active()
            ):
                raise JobConflictError("同じ課題のジョブが既に待機中または実行中です")
            job = {
                "id": uuid.uuid4().hex[:12], "coursework_id": coursework_id,
                "course_id": course_id, "token_ref": token_ref,
                "settings_revision": settings_revision,
                "phase": phase, "status": "queued", "current_step": None,
                "completed_steps": [],
                "created_at": _now(), "created_ns": time.time_ns(),
                "started_at": None, "finished_at": None,
                "options": {"lenient": options.lenient, "force": options.force,
                            "anchor": options.anchor, "stance": options.stance,
                            "allow_partial": options.allow_partial,
                            "allow_stale_meta": options.allow_stale_meta,
                            "hybrid": options.hybrid},
                "log": "", "error": None,
            }
            self.store.save(job)
            if not self._worker_running:
                self._worker_running = True
                start_worker = True
        if start_worker:
            threading.Thread(target=self._worker, daemon=True).start()
        return job

    def _worker(self) -> None:
        try:
            while True:
                with self.lock:
                    queued = [j for j in self.store.list() if j.get("status") == "queued"]
                    if not queued:
                        self._worker_running = False
                        return
                    job = min(queued, key=lambda j: (j.get("created_ns", 0), j["id"]))
                    self.store.update(job["id"], status="running", started_at=_now())
                self._execute(job["id"], claimed=True)
        finally:
            with self.lock:
                self._worker_running = False

    def cancel(self, job_id: str) -> dict[str, Any]:
        """安全に取り消せるqueued jobだけをcancelする。"""
        with self.lock:
            job = self.store.get(job_id)
            if job is None:
                raise KeyError(job_id)
            if job.get("status") != "queued":
                raise ValueError("実行中または終了済みのジョブは取り消せません")
            return self.store.update(
                job_id, status="canceled", finished_at=_now(),
                error="待機中に利用者が取り消しました",
            )

    def _command(
        self, phase: str, cw: str, options: dict[str, Any], *,
        course_id: str | None = None, token_ref: str | None = None,
    ) -> list[str]:
        command_phase = "fetch" if phase == "prepare" else phase
        args = ["python", "-m", "grader", command_phase, "--coursework", cw]
        if course_id:
            args += ["--course-id", course_id]
        if token_ref:
            if self.token_resolver is None:
                raise RuntimeError("利用者トークンの解決先が設定されていません")
            args += ["--token-file", str(self.token_resolver(token_ref))]
        # 採点姿勢: 一次採点(run)と審判(refine)の両方に同じスタンスを適用する。
        # auto(未指定)はフラグを付けず、課題タイプの既定(rubric.DEFAULT_LENIENT_RUBRICS)に任せる
        stance = options.get("stance") or ("lenient" if options.get("lenient") else "auto")
        if phase in {"run", "refine"}:
            if stance == "lenient":
                args.append("--lenient")
            elif stance == "strict":
                args.append("--strict")
        if phase in {"run", "refine"} and options.get("force"):
            args.append("--force")
        if phase == "refine" and options.get("anchor"):
            args += ["--anchor", str(options["anchor"])]
        if phase == "report" and options.get("allow_partial"):
            args.append("--allow-partial")
        if phase == "report" and options.get("allow_stale_meta"):
            args.append("--allow-stale-meta")
        if phase == "hybrid":
            if not token_ref:
                raise RuntimeError("高速採点には利用者参照が必要です")
            args += ["--owner-ref", token_ref]
            if options.get("force"):
                args.append("--force")
        return args

    def _execute(self, job_id: str, *, claimed: bool = False) -> None:
        job = self.store.get(job_id) if claimed else self.store.update(
            job_id, status="running", started_at=_now())
        if job is None:
            return
        if job["phase"] == "full" and job["options"].get("hybrid"):
            # 最新答案を取得してから高速採点案をowner-scoped storeへ保存する。
            phases = ["prepare", "hybrid"]
        elif job["phase"] == "full":
            # 標準はQwen2.5単独の1段階採点。Qwen3審判(refine)はfullへ含めない。
            # refineは診断・比較用として、単独フェーズでのみ実行できる。
            phases = ["run", "report"]
        else:
            phases = [job["phase"]]
        log_text = ""
        completed_steps = list(job.get("completed_steps") or [])
        try:
            if (self.settings_guard and job.get("course_id")
                    and not self.settings_guard(
                        job["course_id"], job["coursework_id"],
                        job.get("settings_revision"),
                    )):
                self.store.update(
                    job_id, status="blocked", finished_at=_now(),
                    error="待機中に採点基準が変更されたため、再確認して作成し直してください",
                )
                return
            for phase in phases:
                model_phase = "run" if phase == "hybrid" else phase
                # 自動切替は一括採点だけで行う。個別run/refineは、従来どおり管理者が
                # 手動で用意したモデルをguardで確認して実行できる診断経路として残す。
                if (job["phase"] == "full" and phase in {"run", "refine", "hybrid"}
                        and self.model_switcher):
                    self.store.update(job_id, current_step=f"model:{model_phase}", log=log_text[-12000:])
                    ok, message = self.model_switcher(model_phase)
                    if not ok:
                        self.store.update(job_id, status="blocked", finished_at=_now(), error=message)
                        return
                if phase in {"run", "refine", "hybrid"} and self.model_guard:
                    ok, message = self.model_guard(model_phase)
                    if not ok:
                        self.store.update(
                            job_id, status="blocked", finished_at=_now(), error=message,
                        )
                        return
                self.store.update(job_id, current_step=phase, log=log_text[-12000:])
                command = self._command(
                    phase, job["coursework_id"], job["options"],
                    course_id=job.get("course_id"), token_ref=job.get("token_ref"),
                )
                try:
                    proc = self.runner(command,
                                       capture_output=True, text=True, timeout=7200)
                except subprocess.TimeoutExpired:
                    self.store.update(
                        job_id, status="failed", finished_at=_now(),
                        error=f"{phase}が制限時間を超えたため停止しました",
                        log=log_text[-12000:],
                    )
                    return
                output = proc.stdout + proc.stderr
                if "--token-file" in command:
                    output = output.replace(command[command.index("--token-file") + 1], "[user-token]")
                log_text += output
                self.store.update(job_id, log=log_text[-12000:])
                if proc.returncode != 0:
                    self.store.update(job_id, status="failed", finished_at=_now(),
                                      error=f"{phase}が終了コード{proc.returncode}で失敗しました")
                    return
                completed_steps.append(phase)
                changes: dict[str, Any] = {
                    "completed_steps": completed_steps, "log": log_text[-12000:],
                }
                if phase == "hybrid":
                    # 採点段階の時刻計測をジョブへ永続化し、進捗APIから参照できるようにする。
                    stamps = _grading_timestamps(proc.stdout)
                    if stamps:
                        changes["grading_timestamps"] = stamps
                    # リクエスト単位計測は要約だけ保存する(生ログは残さない)。
                    timing = _request_timing_summary(proc.stdout)
                    if timing:
                        changes["request_timing_summary"] = timing
                self.store.update(job_id, **changes)
            self.store.update(job_id, status="succeeded", current_step=None,
                              finished_at=_now(), log=log_text[-12000:])
        except Exception as exc:  # noqa: BLE001
            self.store.update(job_id, status="failed", finished_at=_now(),
                              error="採点ジョブの実行中にエラーが発生しました",
                              log=log_text[-12000:])
