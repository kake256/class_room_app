"""VLM採点(vLLM OpenAI互換API、guided_json、2回採点+合議)。"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import pathlib
import time
from typing import Any

from openai import AsyncOpenAI

from .config import Config
from .rubric import GRADING_SCHEMA, SYSTEM_PROMPT, build_user_prompt, resolve_assignment

log = logging.getLogger(__name__)


def _b64_image(path: pathlib.Path) -> dict[str, Any]:
    data = base64.b64encode(path.read_bytes()).decode()
    return {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{data}"}}


def file_sha256(path: str | pathlib.Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def clamp_total(run: dict[str, Any]) -> int:
    """1回分の採点結果から確定合計点(0〜3の整数)を導出する。

    ゲート不通過は0点。totalは観点和と食い違う場合があるため観点和を優先。
    四捨五入(0.5は必ず切り上げ)。Python組み込みのround()は銀行丸め
    (round(0.5)=0, round(2.5)=2)になり、ルーブリックが指示する「四捨五入」と
    食い違うため使わない。
    """
    if not run.get("gate", {}).get("pass", False):
        return 0
    s = sum(float(c.get("score", 0)) for c in run.get("criteria", []))
    return max(0, min(3, int(s + 0.5)))


def consolidate(runs: list[dict[str, Any]]) -> dict[str, Any]:
    """2回(以上)の採点結果を合議する。

    一致 → その点数。不一致 → 低い方 + inconsistent フラグ。
    LLM由来の flags は全run分をマージして引き継ぐ。
    """
    totals = [clamp_total(r) for r in runs]
    flags: list[str] = sorted({f for r in runs for f in r.get("flags", [])})
    if len(set(totals)) == 1:
        final = totals[0]
    else:
        final = min(totals)
        flags.append("inconsistent")
    if any(not r.get("gate", {}).get("pass", True) for r in runs):
        flags.append("gate_failed")
    return {"run_totals": totals, "final_score": final, "flags": sorted(set(flags))}


class Grader:
    def __init__(
        self,
        cfg: Config,
        coursework_id: str | None = None,
        lenient: bool | None = None,
    ):
        self.cfg = cfg
        self.coursework_id = coursework_id
        self.lenient = lenient  # None=厳しめ既定
        # courseWorkId → 課題文・ルーブリックを config の assignments: から解決
        assignments_map = cfg.get("assignments", default={}) or {}
        self.assignment_text, self.rubric_key = resolve_assignment(
            coursework_id, assignments_map
        )
        self.client = AsyncOpenAI(
            base_url=cfg.get("vllm", "base_url", default="http://localhost:8000/v1"),
            api_key=cfg.get("vllm", "api_key", default="EMPTY"),
        )
        self.model = cfg.get("vllm", "model")
        self.runs = int(cfg.get("grading", "runs", default=2))
        self.sem = asyncio.Semaphore(int(cfg.get("grading", "concurrency", default=4)))

    async def grade_once(self, page_paths: list[pathlib.Path]) -> dict[str, Any]:
        content: list[dict[str, Any]] = [
            {"type": "text", "text": build_user_prompt(
                self.assignment_text, self.rubric_key, self.lenient)}
        ]
        content += [_b64_image(p) for p in page_paths]
        async with self.sem:
            resp = await self.client.chat.completions.create(
                model=self.model,
                temperature=float(self.cfg.get("vllm", "temperature", default=0.0)),
                max_tokens=int(self.cfg.get("vllm", "max_tokens", default=1500)),
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": content},
                ],
                extra_body={"guided_json": GRADING_SCHEMA},
            )
        return json.loads(resp.choices[0].message.content)

    async def grade_submission(
        self,
        student_id: str,
        page_paths: list[pathlib.Path],
        result_path: pathlib.Path,
        source_pdf: pathlib.Path | None = None,
        extra_flags: list[str] | None = None,
        force: bool = False,
    ) -> dict[str, Any]:
        """1答案を runs 回採点して results JSON を保存する。

        既存結果があり、元PDFのハッシュが変わっていなければスキップ(冪等)。
        再提出(ハッシュ変化)は自動再採点。
        """
        src_hash = file_sha256(source_pdf) if source_pdf else None
        if result_path.exists() and not force:
            prev = json.loads(result_path.read_text(encoding="utf-8"))
            if prev.get("source_sha256") == src_hash and prev.get("status") == "ok":
                log.info("%s: skipped (already graded)", student_id)
                return prev

        t0 = time.monotonic()
        try:
            runs = list(
                await asyncio.gather(*(self.grade_once(page_paths) for _ in range(self.runs)))
            )
            result: dict[str, Any] = {"status": "ok", "runs": runs, **consolidate(runs)}
        except Exception as e:  # 1答案の失敗でバッチを止めない
            log.error("%s: grading failed: %s", student_id, e)
            result = {"status": "ERROR", "error": str(e), "runs": [], "flags": ["error"]}

        result.update(
            student_id=student_id,
            source_sha256=src_hash,
            n_pages=len(page_paths),
            elapsed_sec=round(time.monotonic() - t0, 1),
            graded_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
        )
        if extra_flags:
            result["flags"] = sorted(set(result.get("flags", [])) | set(extra_flags))
        result_path.parent.mkdir(parents=True, exist_ok=True)
        result_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        return result
