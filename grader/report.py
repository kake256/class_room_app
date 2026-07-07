"""集計・判定区分・遅延減点・3点候補リスト・CSV出力。"""
from __future__ import annotations

import json
import pathlib
import re
from typing import Any

import fitz  # PyMuPDF
import pandas as pd

from .config import Config
from .rubric import COURSE_KEYWORDS, CRITERIA_NAMES, resolve_assignment

REVIEW_FLAGS = {
    "inconsistent",
    "gate_failed",
    "format_violation",
    "pages_truncated",
    "error",
}


def judge_score(result: dict[str, Any]) -> int | None:
    """審判モデル(judge)の確定点。judge未実施・失敗時は None。"""
    j = result.get("judge") or {}
    if j.get("status") == "ok" and j.get("runs"):
        return int(j.get("final_score", 0))
    return None


def classify(result: dict[str, Any], crit_prune: float | None = 2.0) -> str:
    """判定区分: auto_0 / auto_1 / auto_2 / candidate_3 / review。

    レビュー行き: ゲート不通過 / 2回不一致 / 形式違反 / 切り捨て / エラー。
    審判フェーズ未実施(judgeデータなし)の場合のみ、一次採点自身が書いた
    flags(判断に迷った旨の自由記述)もレビュー行きの根拠にする
    (クロスチェックする審判モデルがまだいないため保守的に扱う)。
    審判フェーズ実施済みなら、flagsの自由記述ではなくjudgeとの突き合わせ
    (下記・build_report側のjudge優先ロジック)に絞り込みを委ねる。

    一次採点3点は自動確定せず candidate_3(甘い一次採点を高recallの網として使う)。
    審判フェーズ実施済みなら候補を絞り込む:
    - ペアワイズで両順負け/tie → auto_2 に降格(実測: 降格精度100%)
    - judge観点合計(2回の低い方)が crit_prune 未満 → auto_2 に降格
    - judgeが3点を付けたのに一次採点<3 の答案は candidate_3 に昇格(安全網)
    """
    if result.get("status") != "ok":
        return "review"
    flags = set(result.get("flags", []))
    score = result.get("final_score", 0)
    has_judge = (result.get("judge") or {}).get("status") == "ok"
    if flags & REVIEW_FLAGS:
        return "review"
    if not has_judge and (flags - {"late", "late_waiver_candidate"}):
        return "review"
    if score >= 3:
        verdict = (result.get("pairwise") or {}).get("verdict")
        if verdict == "demote":
            return "auto_2"
        if verdict == "error":
            return "review"
        crit_sum = (result.get("judge") or {}).get("crit_min_sum")
        if (
            crit_prune is not None
            and crit_sum is not None
            and (result.get("judge") or {}).get("status") == "ok"
            and crit_sum < crit_prune
        ):
            return "auto_2"
        return "candidate_3"
    js = judge_score(result)
    if js is not None and js >= 3:  # 審判の方が高評価: 見逃し防止で候補へ
        return "candidate_3"
    return f"auto_{score}"


def candidate_tier(result: dict[str, Any]) -> str:
    """candidate_3 内の格付け。TAは strong から順に確認する。

    - strong: ペアワイズ両順勝ち(人間3点が最も集中する層)
    - borderline: 片順勝ち、または審判が3点を付けた昇格組
    - unrefined: 審判フェーズ未実施
    """
    verdict = (result.get("pairwise") or {}).get("verdict")
    if verdict == "keep_candidate":
        return "strong"
    if verdict == "borderline":
        return "borderline"
    js = judge_score(result)
    if js is not None and js >= 3:
        return "borderline"
    return "unrefined"


def apply_late_policy(score: int, late: bool, penalty: int = 1) -> tuple[int, bool]:
    """遅延減点。(適用後点数, late_waiver_candidate) を返す。

    内容点3点は減点を保留し waiver 候補フラグのみ立てる(TAが個別判断)。
    """
    if not late:
        return score, False
    if score >= 3:
        return score, True
    return max(0, score - penalty), False


def _pdf_text(pdf_path: pathlib.Path) -> str:
    """PDF本文を読み出す。読めなければ空文字(機能を無効化する側に倒す)。"""
    if not pdf_path.exists():
        return ""
    try:
        doc = fitz.open(pdf_path)
        text = "".join(page.get_text() for page in doc)
        doc.close()
    except Exception:  # noqa: BLE001
        return ""
    return text


def _char_count(text: str) -> int:
    """空白を除いた文字数。"""
    return len(re.sub(r"\s", "", text))


def _keyword_hits(text: str) -> int:
    """講義で扱った技術用語(COURSE_KEYWORDS)のうち本文中に出現する語数。"""
    return sum(1 for kw in COURSE_KEYWORDS if kw in text)


def apply_length_bonus(
    r: dict[str, Any], cat: str, cfg: Config, coursework_id: str, student_id: str,
    rubric_key: str | None,
) -> str:
    """感想文系課題(KANSOU/EFFORT)の3点候補を「確定/要確認」に仕分ける。

    一次・judgeとも3点で一致している candidate_3 のうち:
    - judge観点合計(crit_min_sum、2回の低い方)が満点(max_crit_sum)に達している
      → 厳格モデルが2回とも全観点満点。無条件で「確認済みの3点」(auto_3)に格上げ
    - crit_min_sumが僅差(min_crit_sum以上・満点未満)の場合のみ、
      (a)本文が長い(min_chars以上)、または(b)講義の技術用語に複数言及している
      (min_keyword_hits以上)という「具体性ボーナス」で満点相当まで底上げできれば
      同様にauto_3へ。どちらも満たさなければ candidate_3 のまま教員確認に回す
      (実測: 技術用語言及数は 堅い3点で平均6.7語、堅い2点で平均1.9語と判別力を確認済み)

    実験課題(EXPERIMENT)には適用しない。満点(3点)は誤りの影響が大きいため、
    このボーナスで確定できるのは candidate_3 のみ(review・auto_0/1/2には無関係)。
    """
    lb = cfg.get("length_bonus") or {}
    if not lb.get("enabled") or cat != "candidate_3":
        return cat
    if rubric_key not in set(lb.get("rubric_keys", ["KANSOU", "EFFORT"])):
        return cat
    crit_sum = (r.get("judge") or {}).get("crit_min_sum")
    if crit_sum is None:
        return cat
    max_crit_sum = float(lb.get("max_crit_sum", 3.0))
    if crit_sum >= max_crit_sum:
        # judgeが2回とも全観点満点(最も固い3点)。ボーナス不要でそのまま確定
        r["flags"] = sorted(set(r.get("flags", [])) | {"judge_full_marks_confirmed"})
        return "auto_3"
    min_crit_sum = float(lb.get("min_crit_sum", 2.5))
    if crit_sum < min_crit_sum:
        return cat
    pdf_path = cfg.data_dir / "pdf" / coursework_id / f"{student_id}.pdf"
    text = _pdf_text(pdf_path)
    min_chars = int(lb.get("min_chars", 350))
    min_keyword_hits = int(lb.get("min_keyword_hits", 3))
    long_enough = _char_count(text) >= min_chars
    specific_enough = _keyword_hits(text) >= min_keyword_hits
    if not (long_enough or specific_enough):
        return cat
    reason = "length_bonus_confirmed" if long_enough else "keyword_bonus_confirmed"
    r["flags"] = sorted(set(r.get("flags", [])) | {reason})
    return "auto_3"


def _criterion_scores(run: dict[str, Any]) -> dict[str, Any]:
    by_name = {c.get("name"): c for c in run.get("criteria", [])}
    return {n: by_name.get(n, {}).get("score") for n in CRITERIA_NAMES}


def build_report(cfg: Config, coursework_id: str) -> pd.DataFrame:
    results_dir = cfg.data_dir / "results" / coursework_id
    meta_path = cfg.data_dir / "meta" / f"{coursework_id}.jsonl"
    meta: dict[str, dict[str, Any]] = {}
    if meta_path.exists():
        for line in meta_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                m = json.loads(line)
                meta[m["student_id"]] = m

    penalty = int(cfg.get("late_penalty", default=1))
    crit_prune = cfg.get("pairwise", "crit_prune", default=2.0)
    crit_prune = float(crit_prune) if crit_prune is not None else None
    try:
        _, rubric_key = resolve_assignment(coursework_id, cfg.get("assignments"))
    except KeyError:
        rubric_key = None
    rows = []
    for rp in sorted(results_dir.glob("*.json")):
        r = json.loads(rp.read_text(encoding="utf-8"))
        sid = r.get("student_id", rp.stem)
        m = meta.get(sid, {})
        late = bool(m.get("late", False))
        score = int(r.get("final_score", 0))
        cat = classify(r, crit_prune=crit_prune)
        js = judge_score(r)
        if cat == "auto_2" and score >= 3:  # 審判フェーズで降格
            score = 2
            r["flags"] = sorted(set(r.get("flags", [])) | {"pairwise_demoted"})
        elif cat.startswith("auto_") and js is not None:
            # 0/1/2の確定点は審判モデルの方が高精度(実測66% vs 36%)
            score = min(js, 2)
            cat = f"auto_{score}"
        else:
            cat = apply_length_bonus(r, cat, cfg, coursework_id, sid, rubric_key)
        final_after_late, waiver = apply_late_policy(score, late, penalty)
        if waiver:
            r["flags"] = sorted(set(r.get("flags", [])) | {"late_waiver_candidate"})
        row: dict[str, Any] = {
            "student_id": sid,
            "name": m.get("name", ""),
            "state": m.get("state", ""),   # TURNED_IN / RETURNED 等(書き戻し時の安全判定用)
        }
        for i, run in enumerate(r.get("runs", [])[:2], start=1):
            for name, s in _criterion_scores(run).items():
                row[f"{name}_run{i}"] = s
        row.update(
            content_score=score,
            category=cat,
            tier=candidate_tier(r) if cat == "candidate_3" else "",
            judge_score=js,
            late=late,
            score_after_late=final_after_late,
            evidence=" | ".join(
                f'{c.get("name")}: {c.get("evidence", "")}'
                for c in (r.get("runs") or [{}])[0].get("criteria", [])
            ),
            flags=",".join(r.get("flags", [])),
            notable=(r.get("runs") or [{}])[0].get("notable"),
            graded_at=r.get("graded_at"),
        )
        rows.append(row)
    # 未提出者(meta にあるが results がない)
    graded = {row["student_id"] for row in rows}
    for sid, m in meta.items():
        if sid not in graded and m.get("state") not in ("TURNED_IN", "RETURNED"):
            rows.append(
                {"student_id": sid, "name": m.get("name", ""), "category": "not_submitted"}
            )
    return pd.DataFrame(rows)


def print_summary(df: pd.DataFrame) -> None:
    """TAが講義中に見るサマリを標準出力に表示。"""
    counts = df["category"].value_counts() if not df.empty else {}
    print("=== 採点サマリ ===")
    for cat in ["auto_0", "auto_1", "auto_2", "auto_3"]:
        label = "自動確定" if cat != "auto_3" else "自動確定(文章量ボーナス)"
        print(f"{label} {cat[-1]}点: {counts.get(cat, 0)}人")
    def _label(row) -> str:
        return row.get("name") or row["student_id"]

    print(f"3点候補: {counts.get('candidate_3', 0)}人")
    if not df.empty:
        cands = df[df["category"] == "candidate_3"]
        order = {"strong": 0, "borderline": 1, "unrefined": 2}
        if "tier" in cands.columns:
            cands = cands.sort_values("tier", key=lambda s: s.map(order).fillna(9))
        for _, row in cands.iterrows():
            t = row.get("tier") or ""
            print(f"  - [{t}] {_label(row)}: {row.get('evidence', '')}")
    print(f"要レビュー: {counts.get('review', 0)}人")
    if not df.empty:
        for _, row in df[df["category"] == "review"].iterrows():
            print(f"  - {_label(row)}: flags={row.get('flags', '')}")
    print(f"未提出: {counts.get('not_submitted', 0)}人")
    if not df.empty:
        for _, row in df[df["category"] == "not_submitted"].iterrows():
            print(f"  - {_label(row)}")


def run_report(cfg: Config, coursework_id: str) -> pathlib.Path:
    df = build_report(cfg, coursework_id)
    out = cfg.data_dir / "report" / f"{coursework_id}.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(out, index=False)
    print_summary(df)
    print(f"\nCSV: {out}")
    return out
