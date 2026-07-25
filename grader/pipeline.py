"""fetch → render → grade の一括実行と watch モード。"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

from .config import Config
from .course_data import is_human_protected
from .course_settings import settings_fingerprint
from .grade import Grader
from .render import render_pdf

log = logging.getLogger(__name__)


async def grade_all(
    cfg: Config, coursework_id: str, force: bool = False, lenient: bool | None = None,
    *, course_id: str | None = None, include_human_graded: bool = False,
) -> list[dict[str, Any]]:
    """pdf/ 配下の全答案を render → grade。処理済みはスキップ(冪等)。"""
    data = cfg.data_dir
    if course_id:
        from .course_data import CoursePaths
        paths = CoursePaths(cfg, course_id, coursework_id)
        pdf_dir, meta_path = paths.read_path("pdf"), paths.read_path("meta")
        pages_root, results_dir = paths.pages, paths.results
        from .course_settings import load_settings
        lightweight_settings = load_settings(cfg, course_id, coursework_id)
    else:
        pdf_dir = data / "pdf" / coursework_id
        pages_root = data / "pages" / coursework_id
        results_dir = data / "results" / coursework_id
        meta_path = data / "meta" / f"{coursework_id}.jsonl"
        lightweight_settings = None

    meta: dict[str, dict[str, Any]] = {}
    if meta_path.exists():
        for line in meta_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                m = json.loads(line)
                meta[m["student_id"]] = m

    grader = Grader(cfg, coursework_id=coursework_id, lenient=lenient,
                    lightweight_settings=lightweight_settings)
    dpi = int(cfg.get("render", "dpi", default=150))
    max_pages = int(cfg.get("render", "max_pages", default=8))

    tasks = []
    # 形式違反(PDFなし)は0点仮置き+レビュー行きの結果を直接書く
    for sid, m in meta.items():
        if not include_human_graded and is_human_protected(m):
            continue
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
                        "settings_fingerprint": settings_fingerprint(lightweight_settings),
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )

    for pdf in sorted(pdf_dir.glob("*.pdf")):
        sid = pdf.stem
        m = meta.get(sid, {})
        if not include_human_graded and is_human_protected(m):
            continue
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
    lenient: bool | None = None, *, course_id: str | None = None,
    token_file: str | None = None,
) -> None:
    if do_fetch:
        from .fetch import fetch_submissions

        fetch_submissions(cfg, coursework_id, course_id=course_id, token_file=token_file)
    results = asyncio.run(grade_all(
        cfg, coursework_id, force=force, lenient=lenient, course_id=course_id,
    ))
    ok = sum(1 for r in results if r.get("status") == "ok")
    log.info("graded: %d ok / %d total", ok, len(results))


def watch(cfg: Config, coursework_id: str, interval: int = 3600,
          lenient: bool | None = None, *, course_id: str | None = None,
          token_file: str | None = None) -> None:
    """一定間隔で fetch→render→grade を繰り返す。Ctrl-Cで停止。

    処理済みはスキップ、再提出(modifiedTime変化)は自動再採点。
    """
    while True:
        try:
            run_once(cfg, coursework_id, lenient=lenient, course_id=course_id,
                     token_file=token_file)
        except Exception:
            log.exception("watch iteration failed; retrying next interval")
        log.info("sleeping %ds ...", interval)
        time.sleep(interval)
