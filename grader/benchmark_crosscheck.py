"""No-write cross-model benchmark with selective Qwen3 adjudication."""
from __future__ import annotations

import asyncio
import json
import math
import time
from collections import Counter
from typing import Any

from openai import AsyncOpenAI

from .benchmark_2x2 import (
    MODELS, OWNER_RE, _metrics, _prepare, _switch_model, _visual_batches,
)
from .config import Config
from .course_data import CoursePaths
from .course_settings import settings_fingerprint
from .external_grading import ExternalProposalStore, submission_page
from .hybrid_grade import _policy_prompt, _schema, _text_batches
from .rubric import SYSTEM_PROMPT, build_user_prompt, resolve_assignment


def _grading_prompt(
    cfg: Config, coursework_id: str, settings: dict[str, Any] | None,
) -> tuple[str, str]:
    if settings:
        return _policy_prompt(settings), "confirmed_settings"
    assignment, rubric_key = resolve_assignment(
        coursework_id, cfg.get("assignments", default={}) or {})
    legacy = build_user_prompt(assignment, rubric_key)
    return (
        SYSTEM_PROMPT + "\n" + legacy + "\n"
        "この計測ではcriteria形式ではなく、指定されたresults JSONへ総合点を出力します。"
        "internal_scoreは上記ルーブリックのtotalを0〜3の整数にした値です。"
        "confidenceは確信度、reasonは判定理由、evidenceは答案中の短い根拠です。"
        "全item_idを重複なく1回ずつ返してください。",
        "legacy_assignment",
    )


class CrosscheckBatchGrader:
    def __init__(self, cfg: Config, coursework_id: str, settings: dict[str, Any] | None,
                 model: str):
        self.model = model
        self.prompt, self.policy_source = _grading_prompt(cfg, coursework_id, settings)
        self.client = AsyncOpenAI(
            base_url=cfg.get("vllm", "base_url", default="http://localhost:8000/v1"),
            api_key=cfg.get("vllm", "api_key", default="EMPTY"),
        )
        self.sem = asyncio.Semaphore(int(cfg.get("hybrid_grading", "concurrency", default=4)))

    @staticmethod
    def _validate(value: dict[str, Any], keys: list[str]) -> dict[str, dict[str, Any]]:
        results = value.get("results") if isinstance(value, dict) else None
        if not isinstance(results, list) or len(results) != len(keys):
            raise ValueError("crosscheck result count mismatch")
        by_key: dict[str, dict[str, Any]] = {}
        for item in results:
            key = str(item.get("item_id") or "") if isinstance(item, dict) else ""
            score = item.get("internal_score") if isinstance(item, dict) else None
            confidence = item.get("confidence") if isinstance(item, dict) else None
            if (key not in keys or key in by_key or isinstance(score, bool)
                    or not isinstance(score, int) or not 0 <= score <= 3
                    or isinstance(confidence, bool)
                    or not isinstance(confidence, (int, float))
                    or not math.isfinite(float(confidence))
                    or not 0 <= float(confidence) <= 1):
                raise ValueError("crosscheck result value invalid")
            reason = str(item.get("reason") or "").strip()
            evidence = str(item.get("evidence") or "").strip()
            if not reason:
                raise ValueError("crosscheck reason missing")
            by_key[key] = {
                "item_id": key, "internal_score": score,
                "confidence": float(confidence), "reason": reason[:2000],
                "evidence": evidence[:4000],
            }
        if set(by_key) != set(keys):
            raise ValueError("crosscheck result ids incomplete")
        return by_key

    async def _complete(
        self, keys: list[str], content: str | list[dict[str, Any]], *,
        prior: dict[str, dict[str, dict[str, Any]]] | None = None,
    ) -> dict[str, dict[str, Any]]:
        system = self.prompt
        if prior is not None:
            system += (
                "\nこれは独立した2つの初回評価が要確認となった答案の審判です。"
                "評価A/Bの点数・理由・根拠を答案と採点基準に照らして比較してください。"
                "多数決やモデル名の推測ではなく、妥当な最終点を独立に決めてください。"
                "A/Bのどちらにも根拠がなければ別の点数でも構いません。"
                "reasonには比較判断を、evidenceには答案自体の根拠を記録してください。")
        messages = [{"role": "system", "content": system}]
        if prior is not None:
            comparisons = {
                key: {
                    "evaluation_A": {
                        field: prior[key]["primary"].get(field)
                        for field in ("internal_score", "reason", "evidence")
                    } if prior[key].get("primary") else None,
                    "evaluation_B": {
                        field: prior[key]["secondary"].get(field)
                        for field in ("internal_score", "reason", "evidence")
                    } if prior[key].get("secondary") else None,
                } for key in keys
            }
            prefix = "【匿名の初回評価】\n" + json.dumps(
                comparisons, ensure_ascii=False, sort_keys=True) + "\n【答案】\n"
            if isinstance(content, list):
                content = [{"type": "text", "text": prefix}] + content
            else:
                content = prefix + content
        messages.append({"role": "user", "content": content})
        async with self.sem:
            response = await self.client.chat.completions.create(
                model=self.model, temperature=0.0,
                max_tokens=min(3500, 256 + 220 * len(keys)),
                messages=messages, extra_body={"guided_json": _schema(keys)},
            )
        return self._validate(json.loads(response.choices[0].message.content), keys)

    async def grade_text(
        self, items: list[dict[str, Any]], *,
        prior: dict[str, dict[str, dict[str, Any]]] | None = None,
    ) -> dict[str, dict[str, Any]]:
        keys = [item["item_id"] for item in items]
        blocks = "".join(
            f"\n<<<ANSWER {item['item_id']}>>>\n{item['answer_text']}\n"
            f"<<<END ANSWER {item['item_id']}>>>" for item in items)
        try:
            return await self._complete(keys, blocks, prior=prior)
        except Exception:
            if len(items) == 1:
                raise
            midpoint = len(items) // 2
            left, right = await asyncio.gather(
                self.grade_text(items[:midpoint], prior=prior),
                self.grade_text(items[midpoint:], prior=prior),
            )
            return left | right

    async def grade_visual_batch(
        self, items: list[dict[str, Any]], paths: CoursePaths, *,
        prior: dict[str, dict[str, dict[str, Any]]] | None = None,
    ) -> dict[str, dict[str, Any]]:
        keys = [item["item_id"] for item in items]
        content: list[dict[str, Any]] = []
        for item in items:
            key = item["item_id"]
            content.append({
                "type": "text", "text": (
                    f"<<<ANSWER {key}>>>\n以下はこの答案だけに属するページです。"),
            })
            for page_number in range(1, int(item["available_pages"]) + 1):
                page = submission_page(paths, item["row"], page_number)
                if page.get("status") != "ready":
                    raise RuntimeError("visual page unavailable")
                content.extend([
                    {"type": "text", "text": (
                        f"[[ANSWER {key} page {page_number} text]]\n"
                        f"{page.get('text') or ''}")},
                    {"type": "image_url", "image_url": {
                        "url": "data:image/jpeg;base64," + page["image"]["data_base64"]}},
                ])
            content.append({"type": "text", "text": f"<<<END ANSWER {key}>>>"})
        try:
            return await self._complete(keys, content, prior=prior)
        except Exception:
            if len(items) == 1:
                raise
            midpoint = len(items) // 2
            left, right = await asyncio.gather(
                self.grade_visual_batch(items[:midpoint], paths, prior=prior),
                self.grade_visual_batch(items[midpoint:], paths, prior=prior),
            )
            return left | right


async def _run_stage(
    cfg: Config, coursework_id: str, paths: CoursePaths,
    settings: dict[str, Any] | None, items: list[dict[str, Any]], model: str, *,
    max_visual_pages: int,
    prior: dict[str, dict[str, dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    grader = CrosscheckBatchGrader(cfg, coursework_id, settings, model)
    text = [item for item in items if item["status"] == "ready"]
    visual = [
        {**item, "available_pages": min(int(item["available_pages"]), max_visual_pages)}
        for item in items if item["status"] == "visual_required"
    ]
    max_items = int(cfg.get("hybrid_grading", "text_batch_max_items", default=12))
    max_chars = int(cfg.get("hybrid_grading", "text_batch_max_chars", default=10000))
    text_batches = _text_batches(text, max_items=max_items, max_chars=max_chars)
    visual_batches = _visual_batches(visual)
    started = time.monotonic()
    batches = text_batches + visual_batches
    values = await asyncio.gather(
        *([grader.grade_text(batch, prior=prior) for batch in text_batches]
          + [grader.grade_visual_batch(batch, paths, prior=prior)
             for batch in visual_batches]),
        return_exceptions=True,
    )
    results: dict[str, dict[str, Any]] = {}
    errors = 0
    for batch, value in zip(batches, values):
        if isinstance(value, BaseException):
            errors += len(batch)
            continue
        results.update(value)
    return {
        "results": results,
        "elapsed_seconds": round(time.monotonic() - started, 2),
        "text": len(text), "visual": len(visual),
        "text_batches": len(text_batches), "visual_batches": len(visual_batches),
        "errors": errors, "policy_source": grader.policy_source,
    }


def _risk_reasons(
    item: dict[str, Any], primary: dict[str, Any] | None,
    secondary: dict[str, Any] | None, *, max_visual_pages: int,
    confidence_threshold: float,
) -> list[str]:
    reasons: list[str] = []
    if primary is None:
        reasons.append("primary_missing")
    if secondary is None:
        reasons.append("secondary_missing")
    if primary and secondary and primary["internal_score"] != secondary["internal_score"]:
        reasons.append("model_disagreement")
    if any(value and value["confidence"] <= confidence_threshold
           for value in (primary, secondary)):
        reasons.append("low_confidence")
    if any(value and len(str(value.get("evidence") or "").strip()) < 4
           for value in (primary, secondary)):
        reasons.append("weak_evidence")
    if (item["status"] == "visual_required"
            and int(item.get("total_pages") or item.get("available_pages") or 0)
            > min(int(item.get("available_pages") or 0), max_visual_pages)):
        reasons.append("pages_truncated")
    return reasons


def _resolve(
    primary: dict[str, Any] | None, secondary: dict[str, Any] | None,
    adjudicated: dict[str, Any] | None, *, risky: bool,
) -> tuple[int | None, str]:
    if primary and secondary and primary["internal_score"] == secondary["internal_score"]:
        score = int(primary["internal_score"])
        if not risky:
            return score, "initial_agreement"
        if adjudicated and adjudicated["internal_score"] == score:
            return score, "risk_confirmed"
        return None, "risk_disagreement_review"
    initial = [value["internal_score"] for value in (primary, secondary) if value]
    if adjudicated and adjudicated["internal_score"] in initial:
        return int(adjudicated["internal_score"]), "adjudicator_selected_initial"
    return None, "unresolved_review"


def _human_internal_score(settings: dict[str, Any] | None, raw: Any) -> int:
    value = float(raw)
    if not settings:
        return max(0, min(3, int(value + .5)))
    mapping = {score: float(settings["score_mapping"][str(score)]) for score in range(4)}
    # Classroom上の実点を、教師が確認した対応表の最も近い内部点へ戻す。
    # 同距離なら保守的に低い内部点を選ぶ。
    return min(mapping, key=lambda score: (abs(mapping[score] - value), score))


def benchmark_crosscheck(
    cfg: Config, course_id: str, coursework_ids: list[str], *,
    human_mode: bool = False, owner_ref: str | None = None,
    token_file: str | None = None,
) -> dict[str, Any]:
    if owner_ref is not None and not OWNER_RE.fullmatch(owner_ref):
        raise ValueError("owner reference is invalid")
    human_grades: dict[str, dict[str, Any]] = {}
    if human_mode:
        if not token_file:
            raise ValueError("human comparison requires token file")
        from .fetch import fetch_assigned_grades
        human_grades = {
            cw: fetch_assigned_grades(cfg, cw, course_id=course_id, token_file=token_file)
            for cw in coursework_ids
        }
    prepared = {
        cw: _prepare(cfg, course_id, cw, human_mode=human_mode,
                     human_grades=human_grades.get(cw) if human_mode else None)
        for cw in coursework_ids
    }
    if human_mode:
        for cw, (paths, settings, items, _human) in list(prepared.items()):
            converted = {
                item["item_id"]: _human_internal_score(
                    settings, human_grades[cw][str(item["row"].get("student_id") or "")])
                for item in items
            }
            prepared[cw] = (paths, settings, items, converted)
    primary_profile, primary_model = MODELS["primary"]
    elapsed, reused = _switch_model(primary_profile)
    switches = {"primary": {"elapsed_seconds": round(elapsed, 2), "reused": reused}}
    primary: dict[str, dict[str, Any]] = {}
    primary_pages = int(cfg.get("render", "max_pages", default=8))
    for cw, (paths, settings, items, _human) in prepared.items():
        primary[cw] = asyncio.run(_run_stage(
            cfg, cw, paths, settings, items, primary_model,
            max_visual_pages=primary_pages))

    secondary_profile, secondary_model = MODELS["judge"]
    elapsed, reused = _switch_model(secondary_profile)
    switches["secondary"] = {"elapsed_seconds": round(elapsed, 2), "reused": reused}
    secondary: dict[str, dict[str, Any]] = {}
    secondary_pages = int(cfg.get("pairwise", "candidate_max_pages", default=6))
    for cw, (paths, settings, items, _human) in prepared.items():
        secondary[cw] = asyncio.run(_run_stage(
            cfg, cw, paths, settings, items, secondary_model,
            max_visual_pages=secondary_pages))

    threshold = float(cfg.get(
        "hybrid_grading", "review_confidence_threshold", default=0.6))
    risks: dict[str, dict[str, list[str]]] = {}
    adjudication: dict[str, dict[str, Any]] = {}
    for cw, (paths, settings, items, _human) in prepared.items():
        risks[cw] = {
            item["item_id"]: _risk_reasons(
                item, primary[cw]["results"].get(item["item_id"]),
                secondary[cw]["results"].get(item["item_id"]),
                max_visual_pages=secondary_pages, confidence_threshold=threshold)
            for item in items
        }
        targets = [item for item in items if risks[cw][item["item_id"]]]
        prior = {
            item["item_id"]: {
                "primary": primary[cw]["results"].get(item["item_id"]),
                "secondary": secondary[cw]["results"].get(item["item_id"]),
            } for item in targets
        }
        adjudication[cw] = (asyncio.run(_run_stage(
            cfg, cw, paths, settings, targets, secondary_model,
            max_visual_pages=secondary_pages, prior=prior)) if targets else {
                "results": {}, "elapsed_seconds": 0.0, "errors": 0,
                "text": 0, "visual": 0, "text_batches": 0, "visual_batches": 0,
                "policy_source": secondary[cw]["policy_source"],
            })

    summary: dict[str, Any] = {}
    for cw, (_paths, settings, items, human) in prepared.items():
        p, s, a = primary[cw], secondary[cw], adjudication[cw]
        both = sorted(set(p["results"]) & set(s["results"]))
        differences = [
            p["results"][key]["internal_score"] - s["results"][key]["internal_score"]
            for key in both
        ]
        final: dict[str, int] = {}
        review: list[str] = []
        resolutions: Counter[str] = Counter()
        for item in items:
            key = item["item_id"]
            score, resolution = _resolve(
                p["results"].get(key), s["results"].get(key), a["results"].get(key),
                risky=bool(risks[cw][key]))
            resolutions[resolution] += 1
            if score is None:
                review.append(key)
            else:
                final[key] = score
        reason_counts = Counter(reason for values in risks[cw].values() for reason in values)
        entry: dict[str, Any] = {
            "answers": len(items),
            "policy_source": p["policy_source"],
            "content_modes": {"text": p["text"], "visual": p["visual"]},
            "batch_counts": {
                "primary": {"text": p["text_batches"], "visual": p["visual_batches"]},
                "secondary": {"text": s["text_batches"], "visual": s["visual_batches"]},
                "adjudication": {"text": a["text_batches"], "visual": a["visual_batches"]},
            },
            "stage_seconds": {
                "primary": p["elapsed_seconds"], "secondary": s["elapsed_seconds"],
                "adjudication": a["elapsed_seconds"],
            },
            "inference_seconds": round(
                p["elapsed_seconds"] + s["elapsed_seconds"] + a["elapsed_seconds"], 2),
            "primary_secondary_compared": len(both),
            "primary_secondary_agreement_rate": (
                round(sum(value == 0 for value in differences) / len(differences), 4)
                if differences else None),
            "primary_minus_secondary_distribution": dict(
                sorted(Counter(map(str, differences)).items())),
            "primary_distribution": dict(sorted(Counter(
                str(value["internal_score"]) for value in p["results"].values()).items())),
            "secondary_distribution": dict(sorted(Counter(
                str(value["internal_score"]) for value in s["results"].values()).items())),
            "adjudication_targets": sum(bool(value) for value in risks[cw].values()),
            "adjudication_target_reasons": dict(sorted(reason_counts.items())),
            "errors": p["errors"] + s["errors"] + a["errors"],
            "errors_by_stage": {
                "primary": p["errors"], "secondary": s["errors"],
                "adjudication": a["errors"],
            },
            "finalized": len(final), "review_required": len(review),
            "resolution_counts": dict(sorted(resolutions.items())),
            "final_distribution": dict(sorted(Counter(map(str, final.values())).items())),
        }
        if human_mode:
            entry["human_comparison"] = {
                "primary": _metrics({key: value["internal_score"]
                                     for key, value in p["results"].items()}, human),
                "secondary": _metrics({key: value["internal_score"]
                                       for key, value in s["results"].items()}, human),
                "final": _metrics(final, human),
            }
        if owner_ref and settings:
            store = ExternalProposalStore(cfg)
            current = store.current(
                owner_ref, course_id, cw, settings_fingerprint(settings))
            reference: dict[str, int] = {}
            for item in items:
                ref = store.submission_ref(
                    owner_ref, course_id, cw, str(item["row"].get("student_id") or ""))
                if ref in current:
                    reference[item["item_id"]] = int(current[ref]["internal_score"])
            entry["proposal_comparison"] = _metrics(final, reference)
        summary[cw] = entry
    return {"switches": switches, "courseworks": summary, "no_grade_writes": True}


def run_benchmark_crosscheck(
    cfg: Config, course_id: str, coursework_ids: list[str], *,
    human_mode: bool = False, owner_ref: str | None = None,
    token_file: str | None = None,
) -> dict[str, Any]:
    result = benchmark_crosscheck(
        cfg, course_id, coursework_ids, human_mode=human_mode,
        owner_ref=owner_ref, token_file=token_file)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return result
