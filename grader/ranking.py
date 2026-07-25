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


def _confirmed_score(
    row: Mapping[str, Any], review: Mapping[str, Any] | None,
) -> float | None:
    """Return only a teacher-confirmed or human-entered score."""
    if row.get("category") == "not_submitted" or row.get("state") == "NOT_SUBMITTED":
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
            elif students[student_id]["name"] == student_id and row.get("name"):
                students[student_id]["name"] = str(row["name"])
            review = reviews.get(student_id)
            confirmed = _confirmed_score(row, review if isinstance(review, Mapping) else None)
            if confirmed is not None:
                scores_by_student[student_id][coursework_id] = confirmed

    sortable: list[tuple[str, str, float, int, tuple[float | None, ...]]] = []
    for student_id, student in students.items():
        values = tuple(scores_by_student[student_id].get(col.coursework_id) for col in columns)
        confirmed = tuple(value for value in values if value is not None)
        sortable.append((student_id, student["name"], sum(confirmed), len(confirmed), values))
    sortable.sort(key=lambda item: -item[2])

    ranked: list[RankedStudent] = []
    prior_total: float | None = None
    prior_rank = 0
    for position, (student_id, name, total, count, values) in enumerate(sortable, 1):
        rank = prior_rank if prior_total is not None and total == prior_total else position
        ranked.append(RankedStudent(rank, student_id, name, total, count, values))
        prior_total, prior_rank = total, rank
    return RankingTable(tuple(columns), tuple(ranked), rank_style)


__all__ = [
    "CourseworkColumn", "RankedStudent", "RankingTable", "build_ranking",
]
