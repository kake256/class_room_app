"""二段階目(審判フェーズ): 一次採点(2.5-7B)の結果を審判モデル(Qwen3-VL-8B)で絞り込む。

一次採点の甘さは「3点相当を取りこぼさない粗い網」として利用し、
本フェーズで判別力の高い審判モデルが3つの仕事をする:

1. judge再採点: 全答案を絶対評価で2回採点(0/1/2の確定点の精度向上、実測66%)
2. ペアワイズ比較: 一次3点候補をアンカー答案(2点代表例)と中立ラベル・
   提示順入れ替えで比較(両順負け/tieは降格。実測: 降格の精度100%)
3. 閾値プルーニング材料: judge観点合計(2回の低い方)を記録
   (report側で crit_prune 未満の候補を降格)

結果は results JSON に "judge" と "pairwise" フィールドとして追記される。
実行前に審判モデルのvLLMサーバへ切り替えること(./run.sh serve q3-8b)。
"""
from __future__ import annotations

import asyncio
import json
import logging
import pathlib
from typing import Any

from .config import Config
from .grade import Grader, _b64_image, consolidate
from .rubric import CRITERIA_NAMES, resolve_assignment

log = logging.getLogger(__name__)

PAIRWISE_SCHEMA = {
    "type": "object",
    "properties": {
        "winner": {"type": "string", "enum": ["1", "2", "tie"]},
        "reason": {"type": "string"},
    },
    "required": ["winner", "reason"],
}

_PAIRWISE_TAIL = """\

明確な差がなければ tie を選ぶこと。どちらかに肩入れせず厳密に比較すること。
出力はJSONのみ: winner は "1" / "2" / "tie" のいずれか。"""

# 課題タイプ(rubric種別)ごとの比較軸。ペアワイズは一次3点候補を2点アンカーと
# 比較して降格判定するため、比較軸が課題と噛み合っていないと誤降格する。
PAIRWISE_PROMPTS = {
    "EXPERIMENT": """\
2つのレポート答案(答案1、答案2)を比較します。どちらも「機械学習の識別器の
パラメータを変えて識別境界と識別率を調べる」実験課題の提出物です。

次の2つの軸で、総合的にどちらが優れているかを判定してください:

1. 定量的評価: 複数条件の識別率を数値で提示・比較し、最良パラメータを
   識別率とともに明示している充実度
2. 考察の深さ: 観察の言い換えにとどまらず「なぜそうなるか」の原理
   (過学習・汎化・アンサンブル等)まで踏み込んでいるか""" + _PAIRWISE_TAIL,
    "KANSOU": """\
2つのレポート答案(答案1、答案2)を比較します。どちらも「講義のまとめと感想」
または「これまで学んだ機械学習の中で好きな手法とその理由」をまとめる課題の提出物です。
実験の数値やグラフを求める課題ではありません。

次の3つの軸で、総合的にどちらが優れているかを判定してください:

1. まとめの具体性: 具体的なトピック・技術名・事例に触れて内容をまとめているか
   (「AIについて学んだ」のような抽象的な言及にとどまっていないか)
2. 理解の正確さ: 手法や講義内容を自分の言葉で正しく整理・説明できているか
   (講義資料の丸写しや明らかな誤解でないか)
3. 感想の深さ: 「面白かった」等の一般的な感想にとどまらず、自分の意見・疑問・
   今後の学習や将来との結びつけがあるか""" + _PAIRWISE_TAIL,
    "EFFORT": """\
2つのレポート答案(答案1、答案2)を比較します。どちらも「これまで学んだ機械学習の
中で好きな手法とその理由」をまとめる課題の提出物です。取り組み量と概念理解を見ます。
実験の数値やグラフを求める課題ではありません。

次の3つの軸で、総合的にどちらが優れているかを判定してください:

1. テーマへの言及: 選んだ手法・トピックに具体的に言及しているか
2. 概念理解: 手法の仕組み・特徴を講義で扱った概念に沿っておおむね正しく説明して
   いるか(「強そう」等の感覚的な印象や明らかな誤解でないか。平易な説明でよい)
3. 分量と自分の言葉: 数文以上のまとまった分量で、自分の言葉で理由や考えを
   述べているか""" + _PAIRWISE_TAIL,
}

# 後方互換(既定は実験課題)
PAIRWISE_PROMPT = PAIRWISE_PROMPTS["EXPERIMENT"]


def decide_verdict(wins: list[bool]) -> str:
    """順序を入れ替えた比較の勝敗から判定する(見逃し防止優先の保守的ルール)。

    - 両方の提示順で候補が勝ち → keep_candidate(候補維持=最有力)
    - 片方の順でのみ勝ち → borderline(候補維持、境界扱い)
    - どちらの順でも勝てず → demote(auto_2 に降格)
    """
    if not wins:
        return "error"
    if all(wins):
        return "keep_candidate"
    if any(wins):
        return "borderline"
    return "demote"


def judge_crit_min_sum(runs: list[dict[str, Any]]) -> float | None:
    """judge採点の観点合計(各観点は複数runの最小値)を返す。"""
    if not runs:
        return None
    total = 0.0
    for name in CRITERIA_NAMES:
        scores = []
        for r in runs:
            by_name = {c.get("name"): c for c in r.get("criteria", [])}
            s = by_name.get(name, {}).get("score")
            if s is not None:
                scores.append(float(s))
        if scores:
            total += min(scores)
    return total


class PairwiseRefiner:
    def __init__(self, cfg: Config, coursework_id: str | None = None):
        self.grader = Grader(cfg, coursework_id=coursework_id)
        judge_model = cfg.get("pairwise", "model", default=None)
        if judge_model:  # 審判モデルが一次採点と異なる場合の上書き
            self.grader.model = str(judge_model)
        self.cfg = cfg
        self.judge_runs = int(cfg.get("pairwise", "judge_runs", default=2))
        self.anchor_pages_limit = int(cfg.get("pairwise", "anchor_max_pages", default=4))
        self.cand_pages_limit = int(cfg.get("pairwise", "candidate_max_pages", default=6))
        # 課題タイプに応じた比較軸を選ぶ(噛み合わない軸での誤降格を防ぐ)
        try:
            _, rubric_key = resolve_assignment(coursework_id, cfg.get("assignments") or {})
        except Exception:  # noqa: BLE001 未登録等は実験課題の軸にフォールバック
            rubric_key = "EXPERIMENT"
        self.pairwise_prompt = PAIRWISE_PROMPTS.get(rubric_key, PAIRWISE_PROMPTS["EXPERIMENT"])

    async def compare_once(
        self, first: list[pathlib.Path], second: list[pathlib.Path]
    ) -> dict[str, Any]:
        """中立ラベルで2答案を比較する。winner: "1"/"2"/"tie"。"""
        content: list[dict[str, Any]] = [{"type": "text", "text": self.pairwise_prompt}]
        content.append({"type": "text", "text": "\n=== 答案1 ==="})
        content += [_b64_image(p) for p in first]
        content.append({"type": "text", "text": "\n=== 答案2 ==="})
        content += [_b64_image(p) for p in second]
        async with self.grader.sem:
            resp = await self.grader.client.chat.completions.create(
                model=self.grader.model,
                temperature=0.0,
                max_tokens=400,
                messages=[{"role": "user", "content": content}],
                extra_body={"guided_json": PAIRWISE_SCHEMA},
            )
        return json.loads(resp.choices[0].message.content)

    async def judge_grade(self, pages: list[pathlib.Path]) -> dict[str, Any]:
        """審判モデルによる絶対評価の再採点(judge_runs回)。"""
        runs = list(await asyncio.gather(
            *(self.grader.grade_once(pages) for _ in range(self.judge_runs))
        ))
        cons = consolidate(runs)
        return {
            "status": "ok",
            "model": self.grader.model,
            "runs": runs,
            "run_totals": cons["run_totals"],
            "final_score": cons["final_score"],
            "crit_min_sum": judge_crit_min_sum(runs),
            # 注意: judgeのflagsは採点説明を含みがちなので review 判定には使わない
            "flags": cons["flags"],
        }

    async def process_one(
        self,
        result_path: pathlib.Path,
        anchor_pages: list[pathlib.Path],
        cand_pages: list[pathlib.Path],
        is_candidate: bool,
        force: bool = False,
    ) -> dict[str, Any]:
        """1答案分: judge再採点(全員)+ペアワイズ(一次3点候補のみ)。"""
        result = json.loads(result_path.read_text(encoding="utf-8"))
        changed = False

        if force or not (result.get("judge") or {}).get("runs"):
            try:
                result["judge"] = await self.judge_grade(cand_pages)
            except Exception as e:
                log.error("%s: judge grading failed: %s", result_path.stem, e)
                result["judge"] = {"status": "ERROR", "error": str(e), "runs": []}
            changed = True

        if is_candidate and (force or not (result.get("pairwise") or {}).get("runs")):
            try:
                # 提示順バイアス対策: 候補を答案2に置く回と答案1に置く回の両方を行う
                r_ab, r_ba = await asyncio.gather(
                    self.compare_once(anchor_pages, cand_pages),   # 候補=答案2
                    self.compare_once(cand_pages, anchor_pages),   # 候補=答案1
                )
                wins = [r_ab["winner"] == "2", r_ba["winner"] == "1"]
                result["pairwise"] = {
                    "runs": [r_ab, r_ba],
                    "candidate_wins": sum(wins),
                    "verdict": decide_verdict(wins),
                }
            except Exception as e:
                log.error("%s: pairwise failed: %s", result_path.stem, e)
                result["pairwise"] = {"runs": [], "verdict": "error"}
            changed = True

        if changed:
            result_path.write_text(
                json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        return result


def refine_candidates(
    cfg: Config, coursework_id: str, anchor_student: str, force: bool = False
) -> None:
    """審判フェーズを実行する(judge再採点は全答案、ペアワイズは一次3点候補のみ)。"""
    data = cfg.data_dir
    results_dir = data / "results" / coursework_id
    pages_root = data / "pages" / coursework_id

    refiner = PairwiseRefiner(cfg, coursework_id=coursework_id)
    anchor_dir = pages_root / anchor_student
    anchor_pages = sorted(anchor_dir.glob("p*.png"), key=lambda p: int(p.stem[1:]))
    if not anchor_pages:
        raise SystemExit(f"アンカー答案のページ画像がありません: {anchor_dir}")
    anchor_pages = anchor_pages[: refiner.anchor_pages_limit]

    async def run_all():
        tasks = []
        for rp in sorted(results_dir.glob("*.json")):
            r = json.loads(rp.read_text(encoding="utf-8"))
            if r.get("status") != "ok":
                continue
            pages = sorted((pages_root / rp.stem).glob("p*.png"),
                           key=lambda p: int(p.stem[1:]))[: refiner.cand_pages_limit]
            if not pages:
                continue
            is_cand = r.get("final_score", 0) >= 3 and rp.stem != anchor_student
            tasks.append(refiner.process_one(rp, anchor_pages, pages, is_cand, force=force))
        if not tasks:
            log.info("refine: no results to process")
            return []
        return await asyncio.gather(*tasks)

    results = asyncio.run(run_all())
    counts: dict[str, int] = {}
    for r in results:
        v = (r or {}).get("pairwise", {}).get("verdict")
        if v:
            counts[v] = counts.get(v, 0) + 1
    judged = sum(1 for r in results if (r or {}).get("judge", {}).get("status") == "ok")
    log.info("refine done: judged=%d pairwise=%s", judged, counts)
    print(f"審判フェーズ完了: judge再採点={judged}件 ペアワイズ判定={counts}")
