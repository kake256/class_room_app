"""集計・判定区分・遅延減点・3点候補リスト・CSV出力。"""
from __future__ import annotations

import json
import os
import pathlib
import re
import tempfile
import time
from typing import Any

import fitz  # PyMuPDF
import pandas as pd

from .config import Config
from .course_data import is_human_protected
from .course_settings import load_settings, mapped_score, settings_fingerprint
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
    rubric_key: str | None, pdf_dir: pathlib.Path | None = None,
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
    pdf_path = (pdf_dir or (cfg.data_dir / "pdf" / coursework_id)) / f"{student_id}.pdf"
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


def apply_experiment_quota(
    rows: list[dict[str, Any]], cfg: Config, coursework_id: str,
    rubric_key: str | None, crit_by_sid: dict[str, Any], penalty: int,
    pdf_dir: pathlib.Path | None = None,
) -> None:
    """実験系(EXPERIMENT)課題の3点候補を分布較正で仕分ける(config: experiment_quota)。

    演習課題では3点候補が提出数の3〜6割まで膨らみ、全件の教員確認は重い。
    一方で満点の線引きは判断が割れるため(人間確定点との検証で、judge満点
    候補ですら人間3点率33〜60%)、個別一致より「3点の総数を過去実績+数人、
    最大でも提出数のmax_rateに収める」方針で較正する(2026-07-16 教員決定)。

    候補を確信度順(judge観点合計 → pairwise tier → 本文の厚み)に並べ:
      上位 (上限-band)人 → auto_3(quota_confirmed)として自動確定
      続く band人        → candidate_3のまま教員確認(総数の微調整はここで行う)
      それ以降           → auto_2 + quota_demoted + demoted_from_3(下書きのみ・返却保留)
    KANSOU/EFFORT系の長さボーナス(apply_length_bonus)とは独立の仕組み。
    """
    q = cfg.get("experiment_quota") or {}
    quota_keys = set(q.get("rubric_keys", ["EXPERIMENT", "DISTANCE", "KNN"]))
    if not q.get("enabled") or rubric_key not in quota_keys:
        return
    cands = [row for row in rows if row.get("category") == "candidate_3"]
    if not cands:
        return
    n_submitted = sum(1 for row in rows if row.get("category") != "not_submitted")
    cap = int(round(float(q.get("max_rate", 0.30)) * n_submitted))
    band = int(q.get("band", 6))
    auto_n = max(0, cap - band)

    def rank_key(row: dict[str, Any]):
        sid = str(row["student_id"])
        crit = crit_by_sid.get(sid) or 0
        tier = 1 if row.get("tier") == "strong" else 0
        # 本文の厚みは同点決勝にのみ効く(主基準にすると水増しを誘発するため)
        chars = _char_count(
            _pdf_text((pdf_dir or (cfg.data_dir / "pdf" / coursework_id)) / f"{sid}.pdf"))
        return (-crit, -tier, -chars)

    cands.sort(key=rank_key)
    for i, row in enumerate(cands):
        flags = set(filter(None, str(row.get("flags") or "").split(",")))
        if i < auto_n:
            row["category"] = "auto_3"
            flags.add("quota_confirmed")
        elif i < cap:
            continue  # 境界帯: candidate_3のまま教員確認
        else:
            row["category"] = "auto_2"
            row["content_score"] = 2
            row["score_after_late"], _ = apply_late_policy(
                2, bool(row.get("late")), penalty)
            flags.update({"quota_demoted", "demoted_from_3"})
        row["flags"] = ",".join(sorted(flags))


def _criterion_scores(run: dict[str, Any]) -> dict[str, Any]:
    by_name = {c.get("name"): c for c in run.get("criteria", [])}
    return {n: by_name.get(n, {}).get("score") for n in CRITERIA_NAMES}


def _report_paths(cfg: Config, coursework_id: str, course_id: str | None):
    if course_id:
        from .course_data import CoursePaths
        paths = CoursePaths(cfg, course_id, coursework_id)
        return paths.read_path("results"), paths.read_path("meta"), paths.report
    return (
        cfg.data_dir / "results" / coursework_id,
        cfg.data_dir / "meta" / f"{coursework_id}.jsonl",
        cfg.data_dir / "report" / f"{coursework_id}.csv",
    )


def _load_meta(path: pathlib.Path) -> dict[str, dict[str, Any]]:
    meta: dict[str, dict[str, Any]] = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                m = json.loads(line)
                meta[m["student_id"]] = m
    return meta


def report_readiness(
    cfg: Config, coursework_id: str, *, course_id: str | None = None,
) -> dict[str, Any]:
    results_dir, meta_path, report_path = _report_paths(cfg, coursework_id, course_id)
    meta = _load_meta(meta_path)
    expected_fingerprint = settings_fingerprint(
        load_settings(cfg, course_id, coursework_id) if course_id else None)
    submitted = {
        sid for sid, item in meta.items()
        if item.get("state") in {"TURNED_IN", "RETURNED"}
    }
    human = {sid for sid in submitted if is_human_protected(meta[sid])}
    system: set[str] = set()
    for path in results_dir.glob("*.json") if results_dir.exists() else []:
        try:
            result = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if (result.get("status") == "ok"
                and result.get("settings_fingerprint") == expected_fingerprint):
            system.add(str(result.get("student_id", path.stem)))
    missing = submitted - human - system
    system_target = submitted - human
    max_age = int(cfg.get("report", "meta_max_age_seconds", default=900))
    meta_age = (time.time() - meta_path.stat().st_mtime) if meta_path.exists() else None
    meta_stale = meta_age is None or meta_age > max_age
    return {
        "ready": (not meta_stale and not missing
                  and (not system_target or bool(system & system_target))),
        "total": len(submitted), "human_graded": len(human),
        "system_graded": len(system & system_target), "missing": len(missing),
        "system_target": len(system_target), "report_exists": report_path.exists(),
        "meta_synced_at": (
            time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(meta_path.stat().st_mtime))
            if meta_path.exists() else None
        ),
        "meta_stale": meta_stale, "meta_age_seconds": round(meta_age) if meta_age else meta_age,
    }


def build_report(
    cfg: Config, coursework_id: str, *, course_id: str | None = None,
) -> pd.DataFrame:
    results_dir, meta_path, _ = _report_paths(cfg, coursework_id, course_id)
    meta = _load_meta(meta_path)
    settings = load_settings(cfg, course_id, coursework_id) if course_id else None
    expected_fingerprint = settings_fingerprint(settings)
    if course_id:
        from .course_data import CoursePaths
        pdf_dir = CoursePaths(cfg, course_id, coursework_id).read_path("pdf")
    else:
        pdf_dir = cfg.data_dir / "pdf" / coursework_id

    penalty = int(cfg.get("late_penalty", default=1))
    crit_prune = cfg.get("pairwise", "crit_prune", default=2.0)
    crit_prune = float(crit_prune) if crit_prune is not None else None
    try:
        _, rubric_key = resolve_assignment(coursework_id, cfg.get("assignments"))
    except KeyError:
        rubric_key = None
    rows = []
    crit_by_sid: dict[str, Any] = {}
    for rp in sorted(results_dir.glob("*.json")):
        r = json.loads(rp.read_text(encoding="utf-8"))
        if r.get("settings_fingerprint") != expected_fingerprint:
            continue
        sid = r.get("student_id", rp.stem)
        m = meta.get(sid, {})
        if is_human_protected(m):
            continue
        late = bool(m.get("late", False))
        score = int(r.get("final_score", 0))
        cat = classify(r, crit_prune=crit_prune)
        js = judge_score(r)
        crit_by_sid[str(sid)] = (r.get("judge") or {}).get("crit_min_sum")
        if cat == "auto_2" and score >= 3:  # 審判フェーズで降格
            score = 2
            r["flags"] = sorted(set(r.get("flags", [])) | {"pairwise_demoted"})
        elif cat.startswith("auto_") and js is not None:
            # 0/1/2の確定点は審判モデルの方が高精度(実測66% vs 36%)
            score = min(js, 2)
            cat = f"auto_{score}"
        else:
            cat = apply_length_bonus(r, cat, cfg, coursework_id, sid, rubric_key, pdf_dir)
        # 満点の取り逃がし検出: 一次採点またはjudgeのどこかで3点評価が出ていた
        # 答案が3点未満で確定する場合、demoted_from_3フラグを付ける。
        # カテゴリは変えない(自動下書き入力は行う)が、ユーザースクリプトは
        # このフラグの答案を返却対象から外し、TAが事後に抜き取り確認できる。
        # 過去5課題の検証: 人間の満点答案118件中この経路の見逃しは4件、
        # いずれも1点差の境界答案で、reviewに全て回すと確認対象が9割超になるため
        # フラグ方式とした(2026-07-16)
        if cat in {"auto_0", "auto_1", "auto_2"} and score < 3:
            primary_high = any(
                (run.get("total") or 0) >= 3 for run in r.get("runs", [])
            )
            if primary_high or (js is not None and js >= 3):
                r["flags"] = sorted(set(r.get("flags", [])) | {"demoted_from_3"})
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
            mapped_score=(mapped_score(settings, score, late)
                          if settings and settings.get("confirmed") else final_after_late),
            source="system",
            evidence=" | ".join(
                f'{c.get("name")}: {c.get("evidence", "")}'
                for c in (r.get("runs") or [{}])[0].get("criteria", [])
            ),
            flags=",".join(r.get("flags", [])),
            notable=(r.get("runs") or [{}])[0].get("notable"),
            graded_at=r.get("graded_at"),
        )
        rows.append(row)
    # 人間が先に入力した成績は保護し、自動返却対象にしない。
    for sid, m in meta.items():
        human_grade = (m.get("assigned_grade") if m.get("assigned_grade") is not None
                       else m.get("draft_grade"))
        if is_human_protected(m):
            rows.append({
                "student_id": sid, "name": m.get("name", ""),
                "state": m.get("state", ""), "category": "human",
                "source": "human",
                "mapped_score": (float(human_grade) if human_grade is not None else None),
                "score_after_late": None, "flags": "human_grade_protected",
            })
    apply_experiment_quota(rows, cfg, coursework_id, rubric_key, crit_by_sid, penalty, pdf_dir)
    for row in rows:
        if row.get("source") == "system":
            raw = int(row.get("content_score", 0))
            row["mapped_score"] = (
                mapped_score(settings, raw, bool(row.get("late")))
                if settings and settings.get("confirmed") else row.get("score_after_late", raw)
            )
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
        label = "自動確定"
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


def run_report(
    cfg: Config, coursework_id: str, *, course_id: str | None = None,
    allow_partial: bool = False, allow_stale_meta: bool = False,
) -> pathlib.Path:
    readiness = report_readiness(cfg, coursework_id, course_id=course_id)
    if readiness["meta_stale"] and not allow_stale_meta:
        raise RuntimeError(
            "Classroom同期情報が古いため集計できません。fetchを再実行してください"
        )
    if readiness["total"] == 0:
        raise RuntimeError("提出済み・返却済みの対象が0件のため集計できません")
    if readiness["system_target"] > 0 and readiness["system_graded"] == 0:
        raise RuntimeError("未採点対象のシステム採点結果が0件です")
    if not readiness["ready"] and not allow_partial:
        raise RuntimeError(
            f"未処理が{readiness['missing']}件あるため集計できません"
        )
    df = build_report(cfg, coursework_id, course_id=course_id)
    if df.empty or "category" not in df.columns:
        raise RuntimeError("集計結果が空のためCSVを更新しません")
    _, _, out = _report_paths(cfg, coursework_id, course_id)
    out.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{out.name}.", dir=str(out.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            df.to_csv(handle, index=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, out)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
    print_summary(df)
    print(f"\nCSV: {out}")
    return out
