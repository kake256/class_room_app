"""fetch → render → grade の一括実行と watch モード。"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

from .config import Config
from .grade import Grader
from .render import render_pdf

log = logging.getLogger(__name__)


async def grade_all(
    cfg: Config, coursework_id: str, force: bool = False, lenient: bool | None = None
) -> list[dict[str, Any]]:
    """pdf/ 配下の全答案を render → grade。処理済みはスキップ(冪等)。"""
    data = cfg.data_dir
    pdf_dir = data / "pdf" / coursework_id
    pages_root = data / "pages" / coursework_id
    results_dir = data / "results" / coursework_id
    meta_path = data / "meta" / f"{coursework_id}.jsonl"

    meta: dict[str, dict[str, Any]] = {}
    if meta_path.exists():
        for line in meta_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                m = json.loads(line)
                meta[m["student_id"]] = m

    grader = Grader(cfg, coursework_id=coursework_id, lenient=lenient)
    dpi = int(cfg.get("render", "dpi", default=150))
    max_pages = int(cfg.get("render", "max_pages", default=8))

    tasks = []
    # 形式違反(PDFなし)は0点仮置き+レビュー行きの結果を直接書く
    for sid, m in meta.items():
        if m.get("format_violation") and not (results_dir / f"{sid}.json").exists():
            results_dir.mkdir(parents=True, exist_ok=True)
            (results_dir / f"{sid}.json").write_text(
                json.dumps(
                    {
                        "status": "ok",
                        "student_id": sid,
                        "runs": [],
                        "run_totals": [],
                        "final_score": 0,
                        "flags": ["format_violation"],
                        "graded_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )

    for pdf in sorted(pdf_dir.glob("*.pdf")):
        sid = pdf.stem
        rr = render_pdf(pdf, pages_root / sid, dpi=dpi, max_pages=max_pages)
        extra = ["pages_truncated"] if rr.truncated else []
        tasks.append(
            grader.grade_submission(
                sid,
                rr.pages,
                results_dir / f"{sid}.json",
                source_pdf=pdf,
                extra_flags=extra,
                force=force,
            )
        )
    return list(await asyncio.gather(*tasks)) if tasks else []


def run_once(
    cfg: Config, coursework_id: str, do_fetch: bool = True, force: bool = False,
    lenient: bool | None = None,
) -> None:
    if do_fetch:
        from .fetch import fetch_submissions

        fetch_submissions(cfg, coursework_id)
    results = asyncio.run(grade_all(cfg, coursework_id, force=force, lenient=lenient))
    ok = sum(1 for r in results if r.get("status") == "ok")
    log.info("graded: %d ok / %d total", ok, len(results))


def watch(cfg: Config, coursework_id: str, interval: int = 3600,
          lenient: bool | None = None) -> None:
    """一定間隔で fetch→render→grade を繰り返す。Ctrl-Cで停止。

    処理済みはスキップ、再提出(modifiedTime変化)は自動再採点。
    """
    while True:
        try:
            run_once(cfg, coursework_id, lenient=lenient)
        except Exception:
            log.exception("watch iteration failed; retrying next interval")
        log.info("sleeping %ds ...", interval)
        time.sleep(interval)
