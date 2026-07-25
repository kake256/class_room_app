"""No-write VL benchmark: grade once, optionally verify evidence without regrading."""
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
from .benchmark_crosscheck import _grading_prompt, _human_internal_score
from .config import Config
from .course_data import CoursePaths
from .course_settings import settings_fingerprint
from .external_grading import ExternalProposalStore, submission_page
from .hybrid_grade import _text_batches


def _grading_schema(keys: list[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {"results": {
            "type": "array", "minItems": len(keys), "maxItems": len(keys),
            "items": {
                "type": "object",
                "properties": {
                    "item_id": {"type": "string", "enum": keys},
                    "internal_score": {"type": "integer", "minimum": 0, "maximum": 3},
                    "rubric_level": {"type": "string", "enum": ["0", "1", "2", "3"]},
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                    "reason": {"type": "string"},
                    "evidence": {"type": "string"},
                },
                "required": [
                    "item_id", "internal_score", "rubric_level", "confidence",
                    "reason", "evidence",
                ],
                "additionalProperties": False,
            },
        }},
        "required": ["results"], "additionalProperties": False,
    }


def _verification_schema(keys: list[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {"results": {
            "type": "array", "minItems": len(keys), "maxItems": len(keys),
            "items": {
                "type": "object",
                "properties": {
                    "item_id": {"type": "string", "enum": keys},
                    "evidence_exists": {"type": "boolean"},
                    "criterion_consistent": {"type": "boolean"},
                    "visual_coverage_ok": {"type": "boolean"},
                    "boundary": {"type": "boolean"},
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                    "issues": {"type": "array", "items": {"type": "string"}},
                },
                "required": [
                    "item_id", "evidence_exists", "criterion_consistent",
                    "visual_coverage_ok", "boundary", "confidence", "issues",
                ],
                "additionalProperties": False,
            },
        }},
        "required": ["results"], "additionalProperties": False,
    }


def _validate_grade(value: Any, keys: list[str]) -> dict[str, dict[str, Any]]:
    rows = value.get("results") if isinstance(value, dict) else None
    if not isinstance(rows, list) or len(rows) != len(keys):
        raise ValueError("grading result count mismatch")
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        key = str(row.get("item_id") or "") if isinstance(row, dict) else ""
        score = row.get("internal_score") if isinstance(row, dict) else None
        level = str(row.get("rubric_level") or "") if isinstance(row, dict) else ""
        confidence = row.get("confidence") if isinstance(row, dict) else None
        if (key not in keys or key in result or isinstance(score, bool)
                or not isinstance(score, int) or not 0 <= score <= 3
                or level not in {"0", "1", "2", "3"}
                or isinstance(confidence, bool) or not isinstance(confidence, (int, float))
                or not math.isfinite(float(confidence)) or not 0 <= float(confidence) <= 1):
            raise ValueError("grading result invalid")
        reason, evidence = str(row.get("reason") or "").strip(), str(row.get("evidence") or "").strip()
        if not reason:
            raise ValueError("grading reason missing")
        result[key] = {
            "item_id": key, "internal_score": score, "rubric_level": level,
            "confidence": float(confidence), "reason": reason[:2000],
            "evidence": evidence[:4000],
        }
    if set(result) != set(keys):
        raise ValueError("grading result ids incomplete")
    return result


def _validate_verification(value: Any, keys: list[str]) -> dict[str, dict[str, Any]]:
    rows = value.get("results") if isinstance(value, dict) else None
    if not isinstance(rows, list) or len(rows) != len(keys):
        raise ValueError("verification result count mismatch")
    result: dict[str, dict[str, Any]] = {}
    bool_fields = ("evidence_exists", "criterion_consistent", "visual_coverage_ok", "boundary")
    for row in rows:
        key = str(row.get("item_id") or "") if isinstance(row, dict) else ""
        confidence = row.get("confidence") if isinstance(row, dict) else None
        issues = row.get("issues") if isinstance(row, dict) else None
        if (key not in keys or key in result or any(not isinstance(row.get(name), bool) for name in bool_fields)
                or isinstance(confidence, bool) or not isinstance(confidence, (int, float))
                or not math.isfinite(float(confidence)) or not 0 <= float(confidence) <= 1
                or not isinstance(issues, list) or any(not isinstance(issue, str) for issue in issues)):
            raise ValueError("verification result invalid")
        result[key] = {
            "item_id": key, **{name: row[name] for name in bool_fields},
            "confidence": float(confidence),
            "issues": [issue.strip()[:500] for issue in issues if issue.strip()][:20],
        }
    if set(result) != set(keys):
        raise ValueError("verification result ids incomplete")
    return result


class EvidenceVerifyGrader:
    def __init__(self, cfg: Config, coursework_id: str, settings: dict[str, Any] | None, model: str):
        self.model = model
        self.policy, self.policy_source = _grading_prompt(cfg, coursework_id, settings)
        self.client = AsyncOpenAI(
            base_url=cfg.get("vllm", "base_url", default="http://localhost:8000/v1"),
            api_key=cfg.get("vllm", "api_key", default="EMPTY"),
        )
        self.sem = asyncio.Semaphore(int(cfg.get("hybrid_grading", "concurrency", default=4)))

    async def _complete(
        self, keys: list[str], content: str | list[dict[str, Any]], *, verify: bool,
    ) -> dict[str, dict[str, Any]]:
        if verify:
            system = self.policy + (
                "\nあなたは採点結果の検証担当です。点数を再採点・変更・出力してはいけません。"
                "答案と提示された一次結果だけを照合し、根拠が答案に実在するか、点数と基準レベルが整合するか、"
                "必要な図表を見落としていないか、隣接レベルとの境界答案かを検証してください。"
                "issuesは具体的問題だけを短く列挙し、問題がなければ空配列にしてください。")
            schema, validator = _verification_schema(keys), _validate_verification
            max_tokens = min(3500, 256 + 180 * len(keys))
        else:
            system = self.policy + (
                "\n各答案を独立に0〜3点で一度だけ採点してください。reason、答案中に実在するevidence、"
                "対応するrubric_level（文字列0〜3）を必ず返してください。")
            schema, validator = _grading_schema(keys), _validate_grade
            max_tokens = min(4000, 256 + 240 * len(keys))
        async with self.sem:
            response = await self.client.chat.completions.create(
                model=self.model, temperature=0.0, max_tokens=max_tokens,
                messages=[{"role": "system", "content": system}, {"role": "user", "content": content}],
                extra_body={"guided_json": schema},
            )
        return validator(json.loads(response.choices[0].message.content), keys)

    @staticmethod
    def _prior_prefix(items: list[dict[str, Any]], prior: dict[str, dict[str, Any]]) -> str:
        data = {item["item_id"]: prior.get(item["item_id"]) for item in items}
        return "【一次採点結果（点数は変更・再出力禁止）】\n" + json.dumps(
            data, ensure_ascii=False, sort_keys=True) + "\n【答案】\n"

    async def text(
        self, items: list[dict[str, Any]], *,
        prior: dict[str, dict[str, Any]] | None = None,
    ) -> dict[str, dict[str, Any]]:
        keys = [item["item_id"] for item in items]
        blocks = "".join(
            f"\n<<<ANSWER {item['item_id']}>>>\n{item['answer_text']}\n<<<END ANSWER {item['item_id']}>>>"
            for item in items)
        if prior is not None:
            blocks = self._prior_prefix(items, prior) + blocks
        try:
            return await self._complete(keys, blocks, verify=prior is not None)
        except Exception:
            if len(items) == 1:
                raise
            mid = len(items) // 2
            left, right = await asyncio.gather(
                self.text(items[:mid], prior=prior), self.text(items[mid:], prior=prior))
            return left | right

    async def visual(
        self, items: list[dict[str, Any]], paths: CoursePaths, *,
        prior: dict[str, dict[str, Any]] | None = None,
    ) -> dict[str, dict[str, Any]]:
        keys = [item["item_id"] for item in items]
        content: list[dict[str, Any]] = []
        if prior is not None:
            content.append({"type": "text", "text": self._prior_prefix(items, prior)})
        for item in items:
            key = item["item_id"]
            content.append({"type": "text", "text": f"<<<ANSWER {key}>>>"})
            for page_number in range(1, int(item["available_pages"]) + 1):
                page = submission_page(paths, item["row"], page_number)
                if page.get("status") != "ready":
                    raise RuntimeError("visual page unavailable")
                content.extend([
                    {"type": "text", "text": f"[[ANSWER {key} page {page_number} text]]\n{page.get('text') or ''}"},
                    {"type": "image_url", "image_url": {
                        "url": "data:image/jpeg;base64," + page["image"]["data_base64"]}},
                ])
            content.append({"type": "text", "text": f"<<<END ANSWER {key}>>>"})
        try:
            return await self._complete(keys, content, verify=prior is not None)
        except Exception:
            if len(items) == 1:
                raise
            mid = len(items) // 2
            left, right = await asyncio.gather(
                self.visual(items[:mid], paths, prior=prior),
                self.visual(items[mid:], paths, prior=prior))
            return left | right


async def _run_stage(cfg: Config, coursework_id: str, paths: CoursePaths,
                     settings: dict[str, Any] | None, items: list[dict[str, Any]], model: str,
                     *, max_visual_pages: int, prior: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    grader = EvidenceVerifyGrader(cfg, coursework_id, settings, model)
    text = [item for item in items if item["status"] == "ready"]
    visual = [{**item, "available_pages": min(int(item["available_pages"]), max_visual_pages)}
              for item in items if item["status"] == "visual_required"]
    text_batches = _text_batches(
        text,
        max_items=int(cfg.get("hybrid_grading", "text_batch_max_items", default=12)),
        max_chars=int(cfg.get("hybrid_grading", "text_batch_max_chars", default=10000)))
    visual_batches = _visual_batches(visual)
    batches = text_batches + visual_batches
    started = time.monotonic()
    values = await asyncio.gather(
        *([grader.text(batch, prior=prior) for batch in text_batches]
          + [grader.visual(batch, paths, prior=prior) for batch in visual_batches]),
        return_exceptions=True)
    results: dict[str, dict[str, Any]] = {}
    errors = 0
    for batch, value in zip(batches, values):
        if isinstance(value, BaseException):
            errors += len(batch)
        else:
            results.update(value)
    return {
        "results": results, "elapsed_seconds": round(time.monotonic() - started, 2),
        "text": len(text), "visual": len(visual), "text_batches": len(text_batches),
        "visual_batches": len(visual_batches), "errors": errors,
        "policy_source": grader.policy_source,
    }


def _risk_reasons(item: dict[str, Any], grade: dict[str, Any] | None,
                  verification: dict[str, Any] | None, *, max_visual_pages: int,
                  confidence_threshold: float) -> list[str]:
    reasons: list[str] = []
    if grade is None:
        reasons.append("stage1_missing")
    if verification is None:
        reasons.append("verification_missing")
        return reasons
    for field in ("evidence_exists", "criterion_consistent", "visual_coverage_ok"):
        if not verification[field]:
            reasons.append(field + "_false")
    if verification["boundary"]:
        reasons.append("boundary")
    if verification["confidence"] <= confidence_threshold:
        reasons.append("low_verification_confidence")
    if verification["issues"]:
        reasons.append("issues_reported")
    if (item["status"] == "visual_required"
            and int(item.get("total_pages") or item.get("available_pages") or 0)
            > min(int(item.get("available_pages") or 0), max_visual_pages)):
        reasons.append("pages_truncated")
    return reasons


def _comparison(scores: dict[str, int], reference: dict[str, int], risks: dict[str, list[str]]) -> dict[str, Any]:
    risky = {key: score for key, score in scores.items() if risks.get(key)}
    nonrisk = {key: score for key, score in scores.items() if not risks.get(key)}
    keys = set(scores) & set(reference)
    mismatches = {key for key in keys if scores[key] != reference[key]}
    risky_keys = {key for key in keys if risks.get(key)}
    risky_mismatches = mismatches & risky_keys
    exact_by_predicted_score: dict[str, dict[str, Any]] = {}
    for score in sorted(set(scores[key] for key in keys)):
        score_keys = {key for key in keys if scores[key] == score}
        exact = sum(scores[key] == reference[key] for key in score_keys)
        exact_by_predicted_score[str(score)] = {
            "n": len(score_keys),
            "exact": exact,
            "exact_rate": round(exact / len(score_keys), 4) if score_keys else None,
        }
    return {
        "overall": _metrics(scores, reference), "risk": _metrics(risky, reference),
        "nonrisk": _metrics(nonrisk, reference),
        "exact_by_predicted_score": exact_by_predicted_score,
        "risk_precision": round(len(risky_mismatches) / len(risky_keys), 4) if risky_keys else None,
        "mismatch_recall": round(len(risky_mismatches) / len(mismatches), 4) if mismatches else None,
        "reference_mismatches": len(mismatches), "risk_detected_mismatches": len(risky_mismatches),
    }


def benchmark_evidence_verify(cfg: Config, course_id: str, coursework_ids: list[str], *,
                              human_mode: bool = False, owner_ref: str | None = None,
                              token_file: str | None = None, model_role: str = "primary",
                              verify: bool = True) -> dict[str, Any]:
    if owner_ref is not None and not OWNER_RE.fullmatch(owner_ref):
        raise ValueError("owner reference is invalid")
    if human_mode and not token_file:
        raise ValueError("human comparison requires token file")
    human_grades: dict[str, dict[str, Any]] = {}
    if human_mode:
        from .fetch import fetch_assigned_grades
        human_grades = {cw: fetch_assigned_grades(cfg, cw, course_id=course_id, token_file=token_file)
                        for cw in coursework_ids}
    prepared = {cw: _prepare(cfg, course_id, cw, human_mode=human_mode,
                             human_grades=human_grades.get(cw) if human_mode else None)
                for cw in coursework_ids}
    if human_mode:
        for cw, (paths, settings, items, _human) in list(prepared.items()):
            converted = {item["item_id"]: _human_internal_score(
                settings, human_grades[cw][str(item["row"].get("student_id") or "")]) for item in items}
            prepared[cw] = (paths, settings, items, converted)

    if model_role not in MODELS:
        raise ValueError("model role is invalid")
    verification_enabled = verify
    profile, model = MODELS[model_role]
    switch_elapsed, reused = _switch_model(profile)
    max_pages = int(cfg.get("render", "max_pages", default=8))
    stage1, stage2 = {}, {}
    for cw, (paths, settings, items, _reference) in prepared.items():
        stage1[cw] = asyncio.run(_run_stage(
            cfg, cw, paths, settings, items, model, max_visual_pages=max_pages))
        stage2[cw] = (asyncio.run(_run_stage(
            cfg, cw, paths, settings, items, model, max_visual_pages=max_pages,
            prior=stage1[cw]["results"])) if verification_enabled else {
                "results": {}, "elapsed_seconds": 0.0, "text": 0, "visual": 0,
                "text_batches": 0, "visual_batches": 0, "errors": 0,
                "policy_source": stage1[cw]["policy_source"],
            })

    threshold = float(cfg.get("hybrid_grading", "review_confidence_threshold", default=.6))
    summary: dict[str, Any] = {}
    for cw, (_paths, settings, items, human) in prepared.items():
        first, verification = stage1[cw], stage2[cw]
        risks = ({item["item_id"]: _risk_reasons(
            item, first["results"].get(item["item_id"]), verification["results"].get(item["item_id"]),
            max_visual_pages=max_pages, confidence_threshold=threshold) for item in items}
            if verification_enabled else {item["item_id"]: [] for item in items})
        scores = {key: int(value["internal_score"]) for key, value in first["results"].items()}
        reason_counts = Counter(reason for values in risks.values() for reason in values)
        issue_count = sum(len(value["issues"]) for value in verification["results"].values())
        risk_count = sum(bool(value) for value in risks.values())
        entry: dict[str, Any] = {
            "answers": len(items), "policy_source": first["policy_source"],
            "content_modes": {"text": first["text"], "visual": first["visual"]},
            "stage1_seconds": first["elapsed_seconds"], "stage2_seconds": verification["elapsed_seconds"],
            "inference_seconds": round(first["elapsed_seconds"] + verification["elapsed_seconds"], 2),
            "batch_counts_per_stage": {"text": first["text_batches"], "visual": first["visual_batches"]},
            "errors": {"stage1": first["errors"], "stage2": verification["errors"]},
            "score_distribution": dict(sorted(Counter(map(str, scores.values())).items())),
            "rubric_level_distribution": dict(sorted(Counter(
                value["rubric_level"] for value in first["results"].values()).items())),
            "risk": risk_count, "nonrisk": len(items) - risk_count,
            "risk_rate": round(risk_count / len(items), 4) if items else None,
            "risk_reasons": dict(sorted(reason_counts.items())),
            # Free-form issues can quote an answer. Expose only an aggregate count.
            "verification_issue_items": issue_count,
            "stage1_scores_unchanged": True,
            "verification_skipped": not verification_enabled,
        }
        reference: dict[str, int] = {}
        reference_type: str | None = None
        if human_mode:
            reference, reference_type = human, "human"
        elif owner_ref and settings:
            store = ExternalProposalStore(cfg)
            current = store.current(owner_ref, course_id, cw, settings_fingerprint(settings))
            for item in items:
                ref = store.submission_ref(owner_ref, course_id, cw, str(item["row"].get("student_id") or ""))
                if ref in current:
                    reference[item["item_id"]] = int(current[ref]["internal_score"])
            reference_type = "existing_proposal"
        if reference_type:
            entry["comparison_reference"] = reference_type
            entry["comparison"] = _comparison(scores, reference, risks)
        summary[cw] = entry
    return {
        "switch": {"profile": profile, "elapsed_seconds": round(switch_elapsed, 2), "reused": reused},
        "confidence_threshold": threshold, "model_role": model_role,
        "courseworks": summary, "no_grade_writes": True,
    }


def run_benchmark_evidence_verify(cfg: Config, course_id: str, coursework_ids: list[str], *,
                                  human_mode: bool = False, owner_ref: str | None = None,
                                  token_file: str | None = None, model_role: str = "primary",
                                  verify: bool = True) -> dict[str, Any]:
    result = benchmark_evidence_verify(
        cfg, course_id, coursework_ids, human_mode=human_mode,
        owner_ref=owner_ref, token_file=token_file, model_role=model_role, verify=verify)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return result
