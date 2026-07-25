"""No-write benchmark for one primary pass and deterministic risk selection."""
from __future__ import annotations

import asyncio
import json
from collections import Counter
from typing import Any

from .benchmark_2x2 import MODELS, OWNER_RE, _metrics, _prepare, _switch_model
from .benchmark_crosscheck import _human_internal_score, _run_stage
from .config import Config
from .course_settings import settings_fingerprint
from .external_grading import ExternalProposalStore


def _primary_risk_reasons(
    item: dict[str, Any], primary: dict[str, Any] | None, *,
    max_visual_pages: int, confidence_threshold: float,
) -> list[str]:
    """Return material, deterministic reasons for reviewing a primary grade."""
    if primary is None:
        return ["primary_missing"]
    reasons: list[str] = []
    if float(primary["confidence"]) <= confidence_threshold:
        reasons.append("low_confidence")
    if len(str(primary.get("evidence") or "").strip()) < 4:
        reasons.append("weak_evidence")
    pages_seen = min(int(item.get("available_pages") or 0), max_visual_pages)
    if (item.get("status") == "visual_required"
            and int(item.get("total_pages") or item.get("available_pages") or 0)
            > pages_seen):
        reasons.append("pages_truncated")
    if int(primary["internal_score"]) == 0:
        reasons.append("zero_score")
    return reasons


def _comparison_metrics(predicted: dict[str, int], reference: dict[str, int]) -> dict[str, Any]:
    result = _metrics(predicted, reference)
    keys = sorted(set(predicted) & set(reference))
    if keys:
        result["two_or_more_difference_rate"] = round(
            sum(abs(predicted[key] - reference[key]) >= 2 for key in keys) / len(keys), 4)
        result["two_or_more_difference_count"] = sum(
            abs(predicted[key] - reference[key]) >= 2 for key in keys)
    return result


def _group_metrics(
    scores: dict[str, int], reference: dict[str, int], risks: dict[str, list[str]],
) -> dict[str, Any]:
    risky = {key: value for key, value in scores.items() if risks.get(key)}
    nonrisk = {key: value for key, value in scores.items() if not risks.get(key)}
    return {
        "overall": _comparison_metrics(scores, reference),
        "risk": _comparison_metrics(risky, reference),
        "nonrisk": _comparison_metrics(nonrisk, reference),
    }


def benchmark_primary_risk(
    cfg: Config, course_id: str, coursework_ids: list[str], *,
    human_mode: bool = False, owner_ref: str | None = None,
    token_file: str | None = None,
) -> dict[str, Any]:
    if owner_ref is not None and not OWNER_RE.fullmatch(owner_ref):
        raise ValueError("owner reference is invalid")
    if human_mode and not token_file:
        raise ValueError("human comparison requires token file")

    human_grades: dict[str, dict[str, Any]] = {}
    if human_mode:
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

    profile, model = MODELS["primary"]
    switch_elapsed, reused = _switch_model(profile)
    max_pages = int(cfg.get("render", "max_pages", default=8))
    stages: dict[str, dict[str, Any]] = {}
    for cw, (paths, settings, items, _reference) in prepared.items():
        stages[cw] = asyncio.run(_run_stage(
            cfg, cw, paths, settings, items, model, max_visual_pages=max_pages))

    threshold = float(cfg.get(
        "hybrid_grading", "review_confidence_threshold", default=0.6))
    summary: dict[str, Any] = {}
    for cw, (_paths, settings, items, human) in prepared.items():
        stage = stages[cw]
        risks = {
            item["item_id"]: _primary_risk_reasons(
                item, stage["results"].get(item["item_id"]),
                max_visual_pages=max_pages, confidence_threshold=threshold)
            for item in items
        }
        scores = {
            key: int(value["internal_score"]) for key, value in stage["results"].items()
        }
        reason_counts = Counter(reason for values in risks.values() for reason in values)
        risk_count = sum(bool(value) for value in risks.values())
        entry: dict[str, Any] = {
            "answers": len(items),
            "primary_seconds": stage["elapsed_seconds"],
            "policy_source": stage["policy_source"],
            "content_modes": {"text": stage["text"], "visual": stage["visual"]},
            "distribution": dict(sorted(Counter(map(str, scores.values())).items())),
            "risk": risk_count,
            "nonrisk": len(items) - risk_count,
            "risk_rate": round(risk_count / len(items), 4) if items else None,
            "risk_reasons": dict(sorted(reason_counts.items())),
            "errors": stage["errors"],
        }
        reference: dict[str, int] = {}
        reference_type: str | None = None
        if human_mode:
            reference, reference_type = human, "human"
        elif owner_ref and settings:
            store = ExternalProposalStore(cfg)
            current = store.current(
                owner_ref, course_id, cw, settings_fingerprint(settings))
            for item in items:
                ref = store.submission_ref(
                    owner_ref, course_id, cw, str(item["row"].get("student_id") or ""))
                if ref in current:
                    reference[item["item_id"]] = int(current[ref]["internal_score"])
            reference_type = "existing_proposal"
        if reference_type:
            entry["comparison_reference"] = reference_type
            entry["comparison"] = _group_metrics(scores, reference, risks)
        summary[cw] = entry
    return {
        "switch": {"profile": profile, "elapsed_seconds": round(switch_elapsed, 2),
                   "reused": reused},
        "confidence_threshold": threshold,
        "courseworks": summary,
        "no_grade_writes": True,
    }


def run_benchmark_primary_risk(
    cfg: Config, course_id: str, coursework_ids: list[str], *,
    human_mode: bool = False, owner_ref: str | None = None,
    token_file: str | None = None,
) -> dict[str, Any]:
    result = benchmark_primary_risk(
        cfg, course_id, coursework_ids, human_mode=human_mode,
        owner_ref=owner_ref, token_file=token_file)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return result
