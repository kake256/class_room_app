"""自動確定分(auto_0/1/2/3)の下書き点(draftGrade)を Classroom に書き込む。

安全設計:
- 書き込むのは draftGrade のみ(assignedGrade/返却は行わない。学生には見えない)
- 対象は category が auto_0/auto_1/auto_2/auto_3 の学生のみ
  (candidate_3 / review / not_submitted はスキップ=人間が判断)
- 点数は report CSV の score_after_late(遅延減点適用後)
- --dry-run で書き込み内容の確認のみ可能
- 既に draftGrade / assignedGrade が入っている提出はスキップ(上書きしない)

注意: Classroom API は「課題を作成したAPIプロジェクト」以外からの成績書き込みを
拒否する(ProjectPermissionDenied)。教師がUIで作成した課題に対しては失敗する
可能性が高く、その場合は課題作成もAPI経由に移行する必要がある。
"""
from __future__ import annotations

import logging

import pandas as pd

from .config import Config
from .fetch import _course_id, get_services

log = logging.getLogger(__name__)

AUTO_CATEGORIES = {"auto_0", "auto_1", "auto_2", "auto_3"}

# 100点満点課題への変換(内部0〜3点 → 100点スケール)。
# 100点満点課題では内部3点を85点へ変換する。
SCORE_MAP_100 = {0: 70, 1: 75, 2: 80, 3: 85}


def map_score(score: int, max_points: int) -> int:
    """内部スコア(0〜3)を課題の満点スケールに変換する。"""
    if max_points == 3:
        return score
    if max_points == 100:
        return SCORE_MAP_100[int(score)]
    raise SystemExit(
        f"満点 {max_points} 点の課題は未対応です(対応: 3点満点、100点満点)。"
    )


def push_draft_grades(
    cfg: Config, coursework_id: str, dry_run: bool = False,
    include_candidates: bool = False,
) -> None:
    """auto_0/1/2/3(+オプションでcandidate_3)の下書き点を書き込む。

    include_candidates: 3点候補にも基準値(3点満点なら3、100点満点なら85)を
    下書きする。感想文回など「基準値を入れてTAが上積み修正する」運用向け。
    reviewカテゴリは常に書き込まない(人間の判断待ち)。
    """
    report_csv = cfg.data_dir / "report" / f"{coursework_id}.csv"
    if not report_csv.exists():
        raise SystemExit(f"{report_csv} がありません。先に report を実行してください。")
    df = pd.read_csv(report_csv)
    cats = AUTO_CATEGORIES | ({"candidate_3"} if include_candidates else set())
    targets = df[df["category"].isin(cats)].copy()
    if targets.empty:
        print("書き込み対象の学生がいません(auto_0/1/2/3が0人。"
              "3点候補にも基準値を入れる場合は --include-candidates)。")
        return

    svc, _ = get_services(cfg)
    cid = _course_id(cfg)
    cw = svc.courses().courseWork().get(courseId=cid, id=coursework_id).execute()
    max_points = int(cw.get("maxPoints", 0))
    targets["push_score"] = targets["score_after_late"].map(
        lambda s: map_score(int(s), max_points)
    )

    scope = "auto_0/1/2/3+3点候補" if include_candidates else "auto_0/1/2/3のみ"
    print(f"書き込み対象: {len(targets)}人(draftGradeのみ、{scope}、満点{max_points}点)")
    for _, r in targets.iterrows():
        scale = "" if max_points == 3 else f"(内部{int(r.score_after_late)}点→)"
        print(f"  - {r.get('name') or r.student_id}: {scale}{int(r.push_score)}点 ({r.category})")
    if dry_run:
        print("(dry-run: 書き込みは行いません)")
        return

    subs = {}
    page_token = None
    while True:
        resp = svc.courses().courseWork().studentSubmissions().list(
            courseId=cid, courseWorkId=coursework_id, pageToken=page_token
        ).execute()
        for s in resp.get("studentSubmissions", []):
            subs[s["userId"]] = s
        page_token = resp.get("nextPageToken")
        if not page_token:
            break

    written = skipped = failed = 0
    for _, r in targets.iterrows():
        sid = str(r.student_id)
        sub = subs.get(sid)
        if not sub:
            log.warning("%s: 提出オブジェクトが見つからずスキップ", sid)
            skipped += 1
            continue
        if sub.get("draftGrade") is not None or sub.get("assignedGrade") is not None:
            log.info("%s: 既に点数あり(draft=%s assigned=%s)、スキップ",
                     sid, sub.get("draftGrade"), sub.get("assignedGrade"))
            skipped += 1
            continue
        try:
            svc.courses().courseWork().studentSubmissions().patch(
                courseId=cid, courseWorkId=coursework_id, id=sub["id"],
                updateMask="draftGrade",
                body={"draftGrade": int(r.push_score)},
            ).execute()
            written += 1
        except Exception as e:
            failed += 1
            msg = str(e)
            log.error("%s: 書き込み失敗: %s", sid, msg[:200])
            if "ProjectPermissionDenied" in msg or "PERMISSION_DENIED" in msg.upper():
                print("\n!! この課題はUI作成のためAPIからの成績書き込みが拒否されています。")
                print("   API経由で作成した課題でのみ書き込み可能です(docstring参照)。")
                break
    print(f"完了: 書き込み{written}件 / スキップ{skipped}件 / 失敗{failed}件")
