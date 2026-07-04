"""キャリブレーション: ローカルPDF + 人間採点CSVで一致率と速度を実測する。

使い方:
  python -m grader calibrate --dir samples --truth samples/truth.csv [--model <name>]

truth.csv 形式: filename,human_score
PDF以外のファイル(画像単体等)は形式違反として0点扱いで比較する。
"""
from __future__ import annotations

import asyncio
import pathlib
import time

import pandas as pd

from .config import Config
from .grade import Grader
from .render import render_pdf
from .rubric import CRITERIA_NAMES


async def _run(cfg: Config, sample_dir: pathlib.Path, truth: pd.DataFrame, work: pathlib.Path):
    grader = Grader(cfg)
    dpi = int(cfg.get("render", "dpi", default=150))
    max_pages = int(cfg.get("render", "max_pages", default=8))
    rows = []
    t_batch = time.monotonic()

    async def one(fname: str, human: float):
        path = sample_dir / fname
        if not path.exists():
            return {"filename": fname, "human": human, "error": "file not found"}
        if path.suffix.lower() != ".pdf":
            # 形式違反: 0点仮置き
            return {"filename": fname, "human": human, "final": 0, "flags": "format_violation",
                    "elapsed": 0.0}
        rr = render_pdf(path, work / "pages" / path.stem, dpi=dpi, max_pages=max_pages)
        t0 = time.monotonic()
        res = await grader.grade_submission(
            path.stem, rr.pages, work / "results" / f"{path.stem}.json",
            source_pdf=path, force=True,
        )
        row = {
            "filename": fname,
            "human": human,
            "final": res.get("final_score"),
            "run_totals": res.get("run_totals"),
            "flags": ",".join(res.get("flags", [])),
            "elapsed": time.monotonic() - t0,
        }
        for run_i, run in enumerate(res.get("runs", [])[:2], start=1):
            for c in run.get("criteria", []):
                if c.get("name") in CRITERIA_NAMES:
                    row[f'{c["name"]}_r{run_i}'] = c.get("score")
        return row

    rows = await asyncio.gather(*(one(r.filename, r.human_score) for r in truth.itertuples()))
    batch_elapsed = time.monotonic() - t_batch
    return pd.DataFrame(list(rows)), batch_elapsed


def calibrate(cfg: Config, sample_dir: str, truth_csv: str, model: str | None = None) -> None:
    if model:
        cfg.raw.setdefault("vllm", {})["model"] = model
    sample_dir_p = pathlib.Path(sample_dir)
    truth = pd.read_csv(truth_csv)
    work = cfg.data_dir / "calibration" / cfg.get("vllm", "model").replace("/", "_")
    df, batch_elapsed = asyncio.run(_run(cfg, sample_dir_p, truth, work))

    print(f"\n=== キャリブレーション結果 (model={cfg.get('vllm', 'model')}) ===")
    print(df.to_string(index=False))

    ok = df.dropna(subset=["final"])
    if len(ok):
        exact = (ok["final"] == ok["human"]).mean()
        within1 = ((ok["final"] - ok["human"]).abs() <= 1).mean()
        print(f"\n完全一致率: {exact:.0%}  ±1以内一致率: {within1:.0%}  (n={len(ok)})")
        graded = ok[ok["elapsed"] > 0]
        if len(graded):
            per_item = graded["elapsed"].mean()
            conc = int(cfg.get("grading", "concurrency", default=4))
            est80 = 80 * per_item / conc / 60
            print(f"1件あたり平均: {per_item:.1f}s(2回採点込み)")
            print(f"バッチ実測: {batch_elapsed:.1f}s / {len(graded)}件")
            print(f"80件換算見積もり({conc}並列): 約{est80:.0f}分"
                  f" {'✓ 1時間以内' if est80 <= 60 else '✗ 1時間超過 → 並列数増加か7B切替を検討'}")
    out = work / "calibration_report.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(out, index=False)
    print(f"CSV: {out}")


def verify_coursework(
    cfg: Config, coursework_id: str, truth_csv: str | None = None, force: bool = False,
    lenient: bool | None = None,
) -> None:
    """過去課題を fetch→grade し、人間の採点結果と傾向が一致するか検証する。

    正解データ: --truth CSV(student_id / name / email のいずれか + human_score 列)、
    省略時は Classroom の確定済み成績(assignedGrade)を使う。
    """
    from .pipeline import run_once
    from .report import build_report

    run_once(cfg, coursework_id, force=force, lenient=lenient)
    df = build_report(cfg, coursework_id)
    df = df[df["category"] != "not_submitted"].copy()

    if truth_csv:
        truth = pd.read_csv(truth_csv, dtype={"student_id": str})
        key = next((k for k in ("student_id", "email", "name") if k in truth.columns), None)
        if key is None or "human_score" not in truth.columns:
            raise SystemExit(
                "truth CSV には human_score 列と student_id/email/name のいずれかが必要です"
            )
        df = df.merge(truth[[key, "human_score"]], on=key, how="left")
    else:
        from .fetch import fetch_assigned_grades

        grades = fetch_assigned_grades(cfg, coursework_id)
        if not grades:
            raise SystemExit(
                "Classroom に確定済み成績(assignedGrade)がありません。--truth CSV を指定してください"
            )
        df["human_score"] = df["student_id"].map(grades)

    print(f"\n=== 過去課題検証 (courseWorkId={coursework_id}) ===")
    both = df.dropna(subset=["human_score", "content_score"]).copy()
    if both.empty:
        raise SystemExit("正解データと突き合わせられた答案がありません")
    diff = both["content_score"] - both["human_score"]
    print(f"突き合わせ: {len(both)}件 / 採点済み {len(df)}件")
    print(f"完全一致率: {(diff == 0).mean():.0%}  ±1以内一致率: {(diff.abs() <= 1).mean():.0%}")
    print(f"平均差(システム−人間): {diff.mean():+.2f}"
          f"(正=システムが甘い / 負=辛い)")
    mismatch = both[diff != 0][["student_id", "name", "human_score", "content_score",
                                "category", "flags", "evidence"]]
    if len(mismatch):
        print("\n-- 不一致の答案 --")
        print(mismatch.to_string(index=False))
    out = cfg.data_dir / "report" / f"{coursework_id}_verify.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(out, index=False)
    print(f"\nCSV: {out}")
