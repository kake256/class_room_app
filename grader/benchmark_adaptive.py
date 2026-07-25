"""No-write adaptive benchmark: primary x2, then risky answers judged once."""
from __future__ import annotations

import asyncio
import json
import time
from collections import Counter
from typing import Any

from .benchmark_2x2 import (
    MODELS, RubricBatchGrader, _metrics, _prepare, _switch_model,
    _visual_batches,
)
from .config import Config
from .course_data import CoursePaths
from .grade import clamp_total
from .hybrid_grade import _text_batches


def _grade_has_weak_evidence(grade: dict[str, Any]) -> bool:
    criteria = grade.get("criteria")
    return (
        not isinstance(criteria, list)
        or not criteria
        or any(not str(item.get("evidence") or "").strip() for item in criteria
               if isinstance(item, dict))
        or any(not isinstance(item, dict) for item in criteria)
    )


def _risk_reasons(
    item: dict[str, Any], primary_runs: list[dict[str, Any]], *, max_visual_pages: int | None = None,
) -> list[str]:
    reasons: list[str] = []
    if len(primary_runs) != 2:
        reasons.append("primary_missing")
    scores = [clamp_total(grade) for grade in primary_runs]
    if len(scores) == 2 and scores[0] != scores[1]:
        reasons.append("primary_disagreement")
    if any(grade.get("flags") for grade in primary_runs):
        reasons.append("flags")
    if any(_grade_has_weak_evidence(grade) for grade in primary_runs):
        reasons.append("weak_evidence")
    available_pages = int(item.get("available_pages") or 0)
    pages_seen = min(available_pages, max_visual_pages or available_pages)
    if int(item.get("total_pages") or 0) > pages_seen:
        reasons.append("pages_truncated")
    return reasons


def _resolve_scores(primary: list[int], judge: int | None) -> tuple[int | None, str]:
    votes = list(primary)
    if judge is not None:
        votes.append(judge)
    counts = Counter(votes)
    winners = [score for score, count in counts.items() if count >= 2]
    if len(winners) == 1:
        return winners[0], "two_vote_agreement"
    return None, "review_required"


async def _run_passes(
    cfg: Config, coursework_id: str, paths: CoursePaths,
    settings: dict[str, Any] | None, items: list[dict[str, Any]], model: str, *,
    passes: int, max_visual_pages: int,
) -> dict[str, Any]:
    grader = RubricBatchGrader(cfg, coursework_id, settings, model)
    text = [item for item in items if item["status"] == "ready"]
    visual = [
        {**item, "available_pages": min(int(item["available_pages"]), max_visual_pages)}
        for item in items if item["status"] == "visual_required"
    ]
    max_items = int(cfg.get("hybrid_grading", "text_batch_max_items", default=12))
    max_chars = int(cfg.get("hybrid_grading", "text_batch_max_chars", default=10000))
    text_batches = _text_batches(text, max_items=max_items, max_chars=max_chars)
    visual_batches = _visual_batches(visual)
    runs: dict[str, list[dict[str, Any]]] = {item["item_id"]: [] for item in items}
    errors = 0
    started = time.monotonic()
    for _pass in range(passes):
        batches = text_batches + visual_batches
        values = await asyncio.gather(
            *([grader.grade_text(batch) for batch in text_batches]
              + [grader.grade_visual(batch, paths) for batch in visual_batches]),
            return_exceptions=True,
        )
        for batch, value in zip(batches, values):
            if isinstance(value, BaseException):
                errors += len(batch)
                continue
            for key, grade in value.items():
                runs[key].append(grade)
    return {
        "runs": runs,
        "elapsed_seconds": round(time.monotonic() - started, 2),
        "text": len(text), "visual": len(visual),
        "text_batches": len(text_batches), "visual_batches": len(visual_batches),
        "errors": errors,
    }


def benchmark_adaptive(
    cfg: Config, course_id: str, coursework_ids: list[str], *,
    human_mode: bool = False, token_file: str | None = None,
) -> dict[str, Any]:
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
        cw: _prepare(
            cfg, course_id, cw, human_mode=human_mode,
            human_grades=human_grades.get(cw) if human_mode else None,
        ) for cw in coursework_ids
    }
    switches: dict[str, Any] = {}
    primary_profile, primary_model = MODELS["primary"]
    elapsed, reused = _switch_model(primary_profile)
    switches["primary"] = {"elapsed_seconds": round(elapsed, 2), "reused": reused}

    primary_results: dict[str, dict[str, Any]] = {}
    risks: dict[str, dict[str, list[str]]] = {}
    primary_max_pages = int(cfg.get("render", "max_pages", default=8))
    for cw, (paths, settings, items, _human) in prepared.items():
        primary_results[cw] = asyncio.run(_run_passes(
            cfg, cw, paths, settings, items, primary_model,
            passes=2, max_visual_pages=primary_max_pages,
        ))
        risks[cw] = {
            item["item_id"]: _risk_reasons(
                item, primary_results[cw]["runs"][item["item_id"]],
                max_visual_pages=primary_max_pages,
            )
            for item in items
        }

    has_risks = any(any(value.values()) for value in risks.values())
    judge_results: dict[str, dict[str, Any]] = {}
    if has_risks:
        judge_profile, judge_model = MODELS["judge"]
        elapsed, reused = _switch_model(judge_profile)
        switches["judge"] = {"elapsed_seconds": round(elapsed, 2), "reused": reused}
        judge_max_pages = int(cfg.get("pairwise", "candidate_max_pages", default=6))
        for cw, (paths, settings, items, _human) in prepared.items():
            risky = [item for item in items if risks[cw][item["item_id"]]]
            judge_results[cw] = asyncio.run(_run_passes(
                cfg, cw, paths, settings, risky, judge_model,
                passes=1, max_visual_pages=judge_max_pages,
            ))
    else:
        for cw in coursework_ids:
            judge_results[cw] = {"runs": {}, "elapsed_seconds": 0.0, "errors": 0}

    summary: dict[str, Any] = {}
    for cw, (_paths, _settings, items, human) in prepared.items():
        primary, judge = primary_results[cw], judge_results[cw]
        final: dict[str, int] = {}
        review: list[str] = []
        resolution_counts: Counter[str] = Counter()
        for item in items:
            key = item["item_id"]
            primary_scores = [clamp_total(value) for value in primary["runs"].get(key, [])]
            judge_values = judge["runs"].get(key, [])
            judge_score = clamp_total(judge_values[0]) if judge_values else None
            score, resolution = _resolve_scores(primary_scores, judge_score)
            resolution_counts[resolution] += 1
            if score is None:
                review.append(key)
            else:
                final[key] = score
        reason_counts = Counter(
            reason for values in risks[cw].values() for reason in values)
        entry: dict[str, Any] = {
            "answers": len(items),
            "content_modes": {"text": primary["text"], "visual": primary["visual"]},
            "batch_counts_per_primary_pass": {
                "text": primary["text_batches"], "visual": primary["visual_batches"]},
            "primary_2pass_seconds": primary["elapsed_seconds"],
            "judge_targets": sum(bool(value) for value in risks[cw].values()),
            "judge_target_reasons": dict(sorted(reason_counts.items())),
            "judge_1pass_seconds": judge["elapsed_seconds"],
            "inference_seconds": round(
                primary["elapsed_seconds"] + judge["elapsed_seconds"], 2),
            "errors": primary["errors"] + judge["errors"],
            "errors_by_model": {"primary": primary["errors"], "judge": judge["errors"]},
            "finalized": len(final), "review_required": len(review),
            "resolution_counts": dict(sorted(resolution_counts.items())),
            "final_distribution": dict(sorted(Counter(map(str, final.values())).items())),
        }
        if human_mode:
            entry["human_comparison"] = _metrics(final, human)
        summary[cw] = entry
    return {"switches": switches, "courseworks": summary, "no_grade_writes": True}


def run_benchmark_adaptive(
    cfg: Config, course_id: str, coursework_ids: list[str], *,
    human_mode: bool = False, token_file: str | None = None,
) -> dict[str, Any]:
    result = benchmark_adaptive(
        cfg, course_id, coursework_ids, human_mode=human_mode, token_file=token_file)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return result
