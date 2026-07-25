"""Pure domain logic for confirmed-score course rankings.

This module deliberately performs no I/O and emits no logs.  API and UI layers can
load reports/reviews using their existing course-scoped authorization and pass the
resulting mappings to :func:`build_ranking`.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Mapping, Sequence


@dataclass(frozen=True)
class CourseworkColumn:
    coursework_id: str
    title: str


@dataclass(frozen=True)
class RankedStudent:
    rank: int
    student_id: str
    name: str
    total: float
    confirmed_count: int
    scores: tuple[float | None, ...]
    # 提出状況の集計。confirmed_count(確定課題数)とは別に、
    # 提出したか・その課題の最高点だったか・未提出かを保持する。
    submitted_count: int = 0
    top_score_count: int = 0
    not_submitted_count: int = 0


@dataclass(frozen=True)
class RankingTable:
    """Competition-ranked rows and their coursework-column order."""

    courseworks: tuple[CourseworkColumn, ...]
    rows: tuple[RankedStudent, ...]
    rank_style: str = "competition"


def _score(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    score = float(value)
    if not math.isfinite(score) or score < 0:
        return None
    return score


def _is_not_submitted(row: Mapping[str, Any]) -> bool:
    """明示的に未提出と分かる行だけをTrueにする(不明はFalse)。"""
    return row.get("category") == "not_submitted" or row.get("state") == "NOT_SUBMITTED"


def _confirmed_score(
    row: Mapping[str, Any], review: Mapping[str, Any] | None,
) -> float | None:
    """Return only a teacher-confirmed or human-entered score."""
    if _is_not_submitted(row):
        return None
    if review and review.get("status") == "confirmed":
        return _score(review.get("score"))
    if row.get("source") != "human":
        return None
    for field in ("mapped_score", "assigned_grade", "draft_grade", "existing_grade", "human_score"):
        score = _score(row.get(field))
        if score is not None:
            return score
    return None


def build_ranking(
    courseworks: Sequence[Mapping[str, Any]], *, rank_style: str = "competition",
) -> RankingTable:
    """Aggregate multiple course-scoped reports into a stable ranking.

    Each coursework mapping must contain ``coursework_id`` and ``rows``.  It may
    contain ``title`` and a ``reviews`` mapping keyed by student id.  Every report
    row must have ``student_id``; malformed rows are ignored.  All encountered
    students are retained, even if they currently have zero confirmed scores.

    ``competition`` ranking is used: totals ``10, 10, 8`` receive ranks ``1, 1,
    3``.  Python's stable sort preserves first-seen student order within ties.
    """
    if rank_style != "competition":
        raise ValueError("rank_style must be 'competition'")

    columns: list[CourseworkColumn] = []
    seen_courseworks: set[str] = set()
    students: dict[str, dict[str, Any]] = {}
    scores_by_student: dict[str, dict[str, float]] = {}
    not_submitted_by_student: dict[str, set[str]] = {}
    submitted_by_student: dict[str, set[str]] = {}

    for coursework in courseworks:
        coursework_id = str(coursework.get("coursework_id") or "")
        if not coursework_id or coursework_id in seen_courseworks:
            raise ValueError("coursework_id must be non-empty and unique")
        seen_courseworks.add(coursework_id)
        columns.append(CourseworkColumn(coursework_id, str(coursework.get("title") or coursework_id)))
        reviews = coursework.get("reviews") or {}
        if not isinstance(reviews, Mapping):
            raise ValueError("reviews must be a mapping")
        rows = coursework.get("rows") or []
        if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
            raise ValueError("rows must be a sequence")
        seen_in_coursework: set[str] = set()
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            student_id = str(row.get("student_id") or "")
            if not student_id or student_id in seen_in_coursework:
                continue
            seen_in_coursework.add(student_id)
            if student_id not in students:
                students[student_id] = {"name": str(row.get("name") or student_id)}
                scores_by_student[student_id] = {}
                not_submitted_by_student[student_id] = set()
                submitted_by_student[student_id] = set()
            elif students[student_id]["name"] == student_id and row.get("name"):
                students[student_id]["name"] = str(row["name"])
            if _is_not_submitted(row):
                not_submitted_by_student[student_id].add(coursework_id)
            else:
                submitted_by_student[student_id].add(coursework_id)
            review = reviews.get(student_id)
            confirmed = _confirmed_score(row, review if isinstance(review, Mapping) else None)
            if confirmed is not None:
                scores_by_student[student_id][coursework_id] = confirmed

    # 各課題の最高点(受講者中のトップ)。確定点が無い課題はNoneのままにする。
    best_by_coursework: dict[str, float] = {}
    for scores in scores_by_student.values():
        for coursework_id, value in scores.items():
            current = best_by_coursework.get(coursework_id)
            if current is None or value > current:
                best_by_coursework[coursework_id] = value

    sortable: list[tuple[str, str, float, int, tuple[float | None, ...], int, int, int]] = []
    for student_id, student in students.items():
        values = tuple(scores_by_student[student_id].get(col.coursework_id) for col in columns)
        confirmed = tuple(value for value in values if value is not None)
        top_count = sum(
            1 for coursework_id, value in scores_by_student[student_id].items()
            if best_by_coursework.get(coursework_id) is not None
            and value >= best_by_coursework[coursework_id])
        sortable.append((
            student_id, student["name"], sum(confirmed), len(confirmed), values,
            len(submitted_by_student[student_id]), top_count,
            len(not_submitted_by_student[student_id])))
    sortable.sort(key=lambda item: -item[2])

    ranked: list[RankedStudent] = []
    prior_total: float | None = None
    prior_rank = 0
    for position, item in enumerate(sortable, 1):
        student_id, name, total, count, values, submitted, top_count, missing = item
        rank = prior_rank if prior_total is not None and total == prior_total else position
        ranked.append(RankedStudent(
            rank, student_id, name, total, count, values,
            submitted_count=submitted, top_score_count=top_count,
            not_submitted_count=missing))
        prior_total, prior_rank = total, rank
    return RankingTable(tuple(columns), tuple(ranked), rank_style)


__all__ = [
    "CourseworkColumn", "RankedStudent", "RankingTable", "build_ranking",
]
