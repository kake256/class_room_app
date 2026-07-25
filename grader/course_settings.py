"""課題別の軽量採点設定。"""
from __future__ import annotations

import json
import hashlib
import time
from typing import Any

from .config import Config
from .course_data import CoursePaths
from .google_auth import write_token_atomic

MAX_NOTES = 4000
MAX_LEVEL_TEXT = 1200


def validate_settings(value: dict[str, Any], max_points: float) -> dict[str, Any]:
    notes = str(value.get("notes", "")).strip()
    if len(notes) > MAX_NOTES:
        raise ValueError("備考は4000文字以内で入力してください")
    raw_levels = value.get("levels")
    raw_mapping = value.get("score_mapping")
    if not isinstance(raw_levels, dict) or not isinstance(raw_mapping, dict):
        raise ValueError("0/1/2/3の判断説明と実点数は4値すべて必要です")
    levels: dict[str, str] = {}
    mapping: dict[str, float] = {}
    for score in range(4):
        key = str(score)
        text = str(raw_levels.get(key, raw_levels.get(score, ""))).strip()
        if not text or len(text) > MAX_LEVEL_TEXT:
            raise ValueError(f"{score}点の判断説明は1〜{MAX_LEVEL_TEXT}文字で入力してください")
        try:
            actual = float(raw_mapping.get(key, raw_mapping.get(score)))
        except (TypeError, ValueError):
            raise ValueError(f"{score}点のClassroom実点数が不正です") from None
        if actual < 0 or actual > float(max_points):
            raise ValueError(f"実点数は0〜{max_points:g}の範囲で入力してください")
        levels[key] = text
        mapping[key] = actual
    if any(mapping[str(i)] > mapping[str(i + 1)] for i in range(3)):
        raise ValueError("実点数は0→1→2→3点で単調非減少にしてください")
    try:
        late_penalty = float(value.get("late_penalty", 0))
    except (TypeError, ValueError):
        raise ValueError("遅延減点が不正です") from None
    if late_penalty < 0 or late_penalty > float(max_points):
        raise ValueError(f"遅延減点は0〜{max_points:g}の範囲です")
    return {
        "notes": notes, "levels": levels, "score_mapping": mapping,
        "late_penalty": late_penalty, "confirmed": bool(value.get("confirmed", False)),
        "max_points": float(max_points),
    }


def load_settings(cfg: Config, course_id: str, coursework_id: str) -> dict[str, Any] | None:
    path = CoursePaths(cfg, course_id, coursework_id).settings
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def save_settings(
    cfg: Config, course_id: str, coursework_id: str, value: dict[str, Any], *,
    max_points: float, actor_ref: str,
) -> dict[str, Any]:
    clean = validate_settings(value, max_points)
    clean.update(actor_ref=actor_ref, updated_at=time.strftime("%Y-%m-%dT%H:%M:%S%z"))
    write_token_atomic(
        CoursePaths(cfg, course_id, coursework_id).settings,
        json.dumps(clean, ensure_ascii=False, indent=2),
    )
    return clean


def settings_prompt(settings: dict[str, Any]) -> str:
    lines = ["【教師が確認済みの課題別採点条件】"]
    if settings.get("notes"):
        lines.append(f"備考: {settings['notes']}")
    for score in range(4):
        lines.append(f"内部{score}点: {settings['levels'][str(score)]}")
    lines.append("上記の0〜3点条件に従い、課題文を推測で補わないこと。")
    return "\n".join(lines)


def settings_fingerprint(settings: dict[str, Any] | None) -> str | None:
    if not settings or not settings.get("confirmed"):
        return None
    # 採点判断を変える項目だけ。実点mapping/遅延減点は再集計で反映する。
    relevant = {key: settings.get(key) for key in ("notes", "levels", "confirmed")}
    encoded = json.dumps(relevant, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def mapped_score(settings: dict[str, Any] | None, raw_score: int, late: bool) -> float:
    if not settings or not settings.get("confirmed"):
        return float(raw_score)
    score = float(settings["score_mapping"][str(max(0, min(3, int(raw_score))))])
    if late:
        score = max(0.0, score - float(settings.get("late_penalty", 0)))
    return score
