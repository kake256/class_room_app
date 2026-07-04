"""CLI: python -m grader {calibrate,fetch,run,watch,report,grade}"""
from __future__ import annotations

import argparse
import logging

from .config import Config

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def main() -> None:
    p = argparse.ArgumentParser(prog="grader")
    p.add_argument("--config", default="config.yaml")
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("calibrate", help="サンプルPDFで一致率と速度を実測")
    c.add_argument("--dir", required=True)
    c.add_argument("--truth", required=True)
    c.add_argument("--model", default=None)

    sub.add_parser("list", help="コースの課題一覧(courseWorkId確認用)")

    v = sub.add_parser("verify", help="過去課題を採点して人間の成績と傾向比較")
    v.add_argument("--coursework", required=True)
    v.add_argument("--truth", default=None, help="正解CSV(省略時はassignedGradeを使用)")
    v.add_argument("--force", action="store_true")
    v.add_argument("--lenient", action="store_true", help="甘め採点(感想文回など)")
    v.add_argument("--strict", action="store_true", help="厳しめ採点(既定を上書き)")

    pg = sub.add_parser("push-grades", help="auto_0/1/2の下書き点をClassroomへ書き込み")
    pg.add_argument("--coursework", required=True)
    pg.add_argument("--dry-run", action="store_true")
    pg.add_argument("--include-candidates", action="store_true",
                    help="3点候補にも基準値(3点/85点)を下書きする(TAが上積み修正する運用)")

    rf = sub.add_parser("refine", help="3点候補をペアワイズ比較で絞り込み(二段階目)")
    rf.add_argument("--coursework", required=True)
    rf.add_argument("--anchor", default=None, help="基準答案(2点相当)のstudent_id")
    rf.add_argument("--force", action="store_true")

    f = sub.add_parser("fetch", help="提出物の取得のみ")
    f.add_argument("--coursework", required=True)

    g = sub.add_parser("grade", help="取得済みPDFの採点のみ(fetchなし)")
    g.add_argument("--coursework", required=True)
    g.add_argument("--force", action="store_true")
    g.add_argument("--lenient", action="store_true", help="甘め採点(感想文回など)")
    g.add_argument("--strict", action="store_true", help="厳しめ採点(既定を上書き)")

    r = sub.add_parser("run", help="fetch→render→grade を1回実行")
    r.add_argument("--coursework", required=True)
    r.add_argument("--force", action="store_true")
    r.add_argument("--lenient", action="store_true", help="甘め採点(感想文回など)")
    r.add_argument("--strict", action="store_true", help="厳しめ採点(既定を上書き)")

    w = sub.add_parser("watch", help="fetch→render→grade を周期実行")
    w.add_argument("--coursework", required=True)
    w.add_argument("--interval", type=int, default=3600)
    w.add_argument("--lenient", action="store_true", help="甘め採点(感想文回など)")
    w.add_argument("--strict", action="store_true", help="厳しめ採点(既定を上書き)")

    rp = sub.add_parser("report", help="集計CSV出力+サマリ表示")
    rp.add_argument("--coursework", required=True)

    args = p.parse_args()
    cfg = Config.load(args.config)

    # 採点姿勢: --lenient / --strict 指定時はそちら、無指定(None)は課題ごとの既定
    lenient = None
    if getattr(args, "lenient", False):
        lenient = True
    elif getattr(args, "strict", False):
        lenient = False

    # URLからコピーしたBase64形式のIDも受け付ける
    if getattr(args, "coursework", None):
        from .fetch import normalize_gid

        args.coursework = normalize_gid(args.coursework)

    if args.cmd == "calibrate":
        from .calibrate import calibrate

        calibrate(cfg, args.dir, args.truth, model=args.model)
    elif args.cmd == "list":
        from .fetch import list_courseworks

        list_courseworks(cfg)
    elif args.cmd == "verify":
        from .calibrate import verify_coursework

        verify_coursework(cfg, args.coursework, truth_csv=args.truth, force=args.force, lenient=lenient)
    elif args.cmd == "push-grades":
        from .push import push_draft_grades

        push_draft_grades(cfg, args.coursework, dry_run=args.dry_run,
                          include_candidates=args.include_candidates)
    elif args.cmd == "refine":
        from .pairwise import refine_candidates

        anchor = args.anchor or cfg.get("pairwise", "anchor_student_id")
        if not anchor:
            raise SystemExit("--anchor か config.yaml の pairwise.anchor_student_id が必要です")
        refine_candidates(cfg, args.coursework, str(anchor), force=args.force)
    elif args.cmd == "fetch":
        from .fetch import fetch_submissions

        fetch_submissions(cfg, args.coursework)
    elif args.cmd == "grade":
        from .pipeline import run_once

        run_once(cfg, args.coursework, do_fetch=False, force=args.force, lenient=lenient)
    elif args.cmd == "run":
        from .pipeline import run_once

        run_once(cfg, args.coursework, force=args.force, lenient=lenient)
    elif args.cmd == "watch":
        from .pipeline import watch

        watch(cfg, args.coursework, interval=args.interval, lenient=lenient)
    elif args.cmd == "report":
        from .report import run_report

        run_report(cfg, args.coursework)


if __name__ == "__main__":
    main()
