"""CLI: python -m grader {calibrate,fetch,run,hybrid,benchmark-*,watch,report,grade}"""
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

    list_cmd = sub.add_parser("list", help="コースの課題一覧(courseWorkId確認用)")

    v = sub.add_parser("verify", help="過去課題を採点して人間の成績と傾向比較")
    v.add_argument("--coursework", required=True)
    v.add_argument("--truth", default=None, help="正解CSV(省略時はassignedGradeを使用)")
    v.add_argument("--force", action="store_true")
    v.add_argument("--lenient", action="store_true", help="甘め採点(感想文回など)")
    v.add_argument("--strict", action="store_true", help="厳しめ採点(既定を上書き)")

    pg = sub.add_parser("push-grades", help="auto_0/1/2/3の下書き点をClassroomへ書き込み")
    pg.add_argument("--coursework", required=True)
    pg.add_argument("--dry-run", action="store_true")
    pg.add_argument("--include-candidates", action="store_true",
                    help="3点候補にも基準値(3点/85点)を下書きする(TAが上積み修正する運用)")

    rf = sub.add_parser("refine", help="3点候補をペアワイズ比較で絞り込み(二段階目)")
    rf.add_argument("--coursework", required=True)
    rf.add_argument("--anchor", default=None, help="基準答案(2点相当)のstudent_id")
    rf.add_argument("--force", action="store_true")
    rf.add_argument("--lenient", action="store_true", help="judgeも甘め採点(一次と合わせる)")
    rf.add_argument("--strict", action="store_true", help="judgeを厳しめ採点(既定を上書き)")

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

    h = sub.add_parser("hybrid", help="テキストバッチ+画像fallbackの高速採点")
    h.add_argument("--coursework", required=True)
    h.add_argument("--owner-ref", required=True)
    h.add_argument("--force", action="store_true")
    h.add_argument("--no-save", action="store_true", help="計測のみで採点案を保存しない")

    b = sub.add_parser("benchmark-2x2", help="一次2回+審判2回を非保存で比較計測")
    b.add_argument("--coursework", required=True, action="append")
    b.add_argument("--course-id", required=True)
    b.add_argument("--human", action="store_true", help="人手採点済み答案を比較対象にする")
    b.add_argument("--owner-ref", default=None, help="既存提案との匿名比較にだけ使用")
    b.add_argument("--token-file", default=None, help="人手確定点を読み取る利用者OAuth token")

    ba = sub.add_parser("benchmark-adaptive", help="一次2回+リスク答案だけ審判1回を非保存で比較計測")
    ba.add_argument("--coursework", required=True, action="append")
    ba.add_argument("--course-id", required=True)
    ba.add_argument("--human", action="store_true", help="人手採点済み答案を比較対象にする")
    ba.add_argument("--token-file", default=None, help="人手確定点を読み取る利用者OAuth token")

    bc = sub.add_parser(
        "benchmark-crosscheck",
        help="一次1回+二次1回+要確認答案だけ二次審判を非保存で比較計測")
    bc.add_argument("--coursework", required=True, action="append")
    bc.add_argument("--course-id", required=True)
    bc.add_argument("--human", action="store_true", help="人手採点済み答案を比較対象にする")
    bc.add_argument("--owner-ref", default=None, help="既存提案との匿名比較にだけ使用")
    bc.add_argument("--token-file", default=None, help="人手確定点を読み取る利用者OAuth token")

    bp = sub.add_parser(
        "benchmark-primary-risk",
        help="一次モデル1回のリスク群・非リスク群を非保存で比較計測")
    bp.add_argument("--coursework", required=True, action="append")
    bp.add_argument("--course-id", required=True)
    bp.add_argument("--human", action="store_true", help="人手採点済み答案を比較対象にする")
    bp.add_argument("--owner-ref", default=None, help="既存提案との匿名比較にだけ使用")
    bp.add_argument("--token-file", default=None, help="人手確定点を読み取る利用者OAuth token")

    bev = sub.add_parser(
        "benchmark-evidence-verify",
        help="一次採点1回+点数を変えない根拠検証1回を非保存で比較計測")
    bev.add_argument("--coursework", required=True, action="append")
    bev.add_argument("--course-id", required=True)
    bev.add_argument("--human", action="store_true", help="人手採点済み答案を比較対象にする")
    bev.add_argument("--owner-ref", default=None, help="既存提案との匿名比較にだけ使用")
    bev.add_argument("--token-file", default=None, help="人手確定点を読み取る利用者OAuth token")
    bev.add_argument("--model-role", choices=("primary", "judge", "minicpm"), default="primary",
                     help="計測に使うモデル役割(primary=Qwen2.5, judge=Qwen3, minicpm=MiniCPM-V-4.5)")
    bev.add_argument("--first-stage-only", action="store_true",
                     help="根拠付き一次採点だけを計測し、二段目の検証を省略")

    w = sub.add_parser("watch", help="fetch→render→grade を周期実行")
    w.add_argument("--coursework", required=True)
    w.add_argument("--interval", type=int, default=3600)
    w.add_argument("--lenient", action="store_true", help="甘め採点(感想文回など)")
    w.add_argument("--strict", action="store_true", help="厳しめ採点(既定を上書き)")

    rp = sub.add_parser("report", help="集計CSV出力+サマリ表示")
    rp.add_argument("--coursework", required=True)
    rp.add_argument("--allow-partial", action="store_true",
                    help="未処理がある部分集計を危険を理解して許可")
    rp.add_argument("--allow-stale-meta", action="store_true",
                    help="古いClassroom同期情報での集計を明示許可")

    # Webジョブは利用者と選択コースに対応する値を明示する。
    # 省略時は従来どおりconfig.yaml / token.jsonを使う。
    for parser in (list_cmd, f, g, r, h, w, rf, rp):
        parser.add_argument("--course-id", default=None)
        parser.add_argument("--token-file", default=None)

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

        args.coursework = (
            [normalize_gid(value) for value in args.coursework]
            if isinstance(args.coursework, list) else normalize_gid(args.coursework)
        )

    if args.cmd == "calibrate":
        from .calibrate import calibrate

        calibrate(cfg, args.dir, args.truth, model=args.model)
    elif args.cmd == "list":
        from .fetch import list_courseworks

        list_courseworks(cfg, course_id=args.course_id, token_file=args.token_file)
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
        refine_candidates(cfg, args.coursework, str(anchor), force=args.force,
                          lenient=lenient, course_id=args.course_id)
    elif args.cmd == "fetch":
        from .fetch import fetch_submissions

        fetch_submissions(cfg, args.coursework, course_id=args.course_id,
                          token_file=args.token_file)
    elif args.cmd == "grade":
        from .pipeline import run_once

        run_once(cfg, args.coursework, do_fetch=False, force=args.force, lenient=lenient)
    elif args.cmd == "run":
        from .pipeline import run_once

        run_once(cfg, args.coursework, force=args.force, lenient=lenient,
                 course_id=args.course_id, token_file=args.token_file)
    elif args.cmd == "hybrid":
        if not args.course_id:
            raise SystemExit("hybridには--course-idが必要です")
        from .hybrid_grade import run_hybrid

        run_hybrid(cfg, args.course_id, args.coursework, args.owner_ref,
                   force=args.force, save=not args.no_save)
    elif args.cmd == "benchmark-2x2":
        from .benchmark_2x2 import run_benchmark_2x2

        run_benchmark_2x2(
            cfg, args.course_id, args.coursework,
            human_mode=args.human, owner_ref=args.owner_ref,
            token_file=args.token_file)
    elif args.cmd == "benchmark-adaptive":
        from .benchmark_adaptive import run_benchmark_adaptive

        run_benchmark_adaptive(
            cfg, args.course_id, args.coursework,
            human_mode=args.human, token_file=args.token_file)
    elif args.cmd == "benchmark-crosscheck":
        from .benchmark_crosscheck import run_benchmark_crosscheck

        run_benchmark_crosscheck(
            cfg, args.course_id, args.coursework,
            human_mode=args.human, owner_ref=args.owner_ref,
            token_file=args.token_file)
    elif args.cmd == "benchmark-primary-risk":
        from .benchmark_primary_risk import run_benchmark_primary_risk

        run_benchmark_primary_risk(
            cfg, args.course_id, args.coursework,
            human_mode=args.human, owner_ref=args.owner_ref,
            token_file=args.token_file)
    elif args.cmd == "benchmark-evidence-verify":
        from .benchmark_evidence_verify import run_benchmark_evidence_verify

        run_benchmark_evidence_verify(
            cfg, args.course_id, args.coursework,
            human_mode=args.human, owner_ref=args.owner_ref,
            token_file=args.token_file, model_role=args.model_role,
            verify=not args.first_stage_only)
    elif args.cmd == "watch":
        from .pipeline import watch

        watch(cfg, args.coursework, interval=args.interval, lenient=lenient,
              course_id=args.course_id, token_file=args.token_file)
    elif args.cmd == "report":
        from .report import run_report

        run_report(cfg, args.coursework, course_id=args.course_id,
                   allow_partial=args.allow_partial,
                   allow_stale_meta=args.allow_stale_meta)


if __name__ == "__main__":
    main()
