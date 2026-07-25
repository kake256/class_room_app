"""Pure domain logic for confirmed-score course rankings.

This module deliberately performs no I/O and emits no logs.  API and UI layers can
load reports/reviews using their existing course-scoped authorization and pass the
resulting mappings to :func:`build_ranking`.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Mapping, Sequence


# 未提出のペナルティ。「3点満点中の-1点」と同じ比重(GAS版の採点方式に合わせる)。
MISSING_PENALTY_RATE = -1 / 3


@dataclass(frozen=True)
class CourseworkColumn:
    coursework_id: str
    title: str
    max_points: float | None = None


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
    # 満点で正規化した平均点(未提出はMISSING_PENALTY_RATE)。順位はこの値で決まる。
    average_rate: float = 0.0
    # 判定できた課題数(確定点あり + 未提出)。未確定は母数へ入れない。
    evaluated_count: int = 0
    unconfirmed_count: int = 0
    # 列ごとに「未提出」かどうか(未確定と区別して表示するため)
    not_submitted_flags: tuple[bool, ...] = ()


@dataclass(frozen=True)
class TopScorer:
    """ある課題で最高点だった学生。"""
    student_id: str
    name: str
    score: float


@dataclass(frozen=True)
class CourseworkTopScore:
    """課題ごとの最高点と、その取得者(同点は全員)。"""
    coursework_id: str
    title: str
    max_points: float | None
    top_score: float | None
    scorers: tuple[TopScorer, ...]


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
        max_points = coursework.get("max_points")
        try:
            max_points = float(max_points) if max_points is not None else None
        except (TypeError, ValueError):
            max_points = None
        columns.append(CourseworkColumn(
            coursework_id, str(coursework.get("title") or coursework_id), max_points))
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

    entries: list[dict[str, Any]] = []
    for student_id, student in students.items():
        values = tuple(scores_by_student[student_id].get(col.coursework_id) for col in columns)
        missing_flags = tuple(
            col.coursework_id in not_submitted_by_student[student_id] for col in columns)
        confirmed = tuple(value for value in values if value is not None)
        top_count = sum(
            1 for coursework_id, value in scores_by_student[student_id].items()
            if best_by_coursework.get(coursework_id) is not None
            and value >= best_by_coursework[coursework_id])
        # 満点で正規化した比率を足す。未提出はペナルティ、未確定は母数へ入れない。
        total_rate = 0.0
        evaluated = 0
        for column, value, missing in zip(columns, values, missing_flags):
            if value is not None:
                maximum = column.max_points
                if maximum is None or maximum <= 0:
                    # 満点が不明な課題は、受講者中の最高点を分母にして正規化する。
                    # (満点が取れないと全員の平均が0になり順位が崩れるため)
                    maximum = best_by_coursework.get(column.coursework_id)
                if maximum is None or maximum <= 0:
                    continue
                total_rate += value / maximum
                evaluated += 1
            elif missing:
                total_rate += MISSING_PENALTY_RATE
                evaluated += 1
        average_rate = (total_rate / evaluated) if evaluated else 0.0
        entries.append({
            "student_id": student_id, "name": student["name"],
            "total": sum(confirmed), "confirmed_count": len(confirmed),
            "values": values, "missing_flags": missing_flags,
            "submitted": len(submitted_by_student[student_id]),
            "top_count": top_count,
            "missing": len(not_submitted_by_student[student_id]),
            "average_rate": average_rate, "evaluated": evaluated,
            "unconfirmed": max(0, len(submitted_by_student[student_id]) - len(confirmed)),
        })
    # 平均点の降順。同率なら最高点回数の多い方を上位にする(GAS版と同じ)。
    entries.sort(key=lambda item: (-item["average_rate"], -item["top_count"]))

    ranked: list[RankedStudent] = []
    prior_key: tuple[float, int] | None = None
    prior_rank = 0
    for position, item in enumerate(entries, 1):
        key = (item["average_rate"], item["top_count"])
        rank = prior_rank if prior_key is not None and key == prior_key else position
        ranked.append(RankedStudent(
            rank, item["student_id"], item["name"], item["total"],
            item["confirmed_count"], item["values"],
            submitted_count=item["submitted"], top_score_count=item["top_count"],
            not_submitted_count=item["missing"],
            average_rate=item["average_rate"], evaluated_count=item["evaluated"],
            unconfirmed_count=item["unconfirmed"],
            not_submitted_flags=item["missing_flags"]))
        prior_key, prior_rank = key, rank
    return RankingTable(tuple(columns), tuple(ranked), rank_style)


def top_scorers(table: RankingTable) -> tuple[CourseworkTopScore, ...]:
    """課題ごとの最高点と取得者を、ランキング表から導出する。

    確定点が1件も無い課題はtop_score=None・取得者なしで返す(欠番にしない)。
    """
    results: list[CourseworkTopScore] = []
    for index, column in enumerate(table.courseworks):
        best: float | None = None
        for row in table.rows:
            value = row.scores[index]
            if value is None:
                continue
            if best is None or value > best:
                best = value
        scorers = tuple(
            TopScorer(row.student_id, row.name, row.scores[index])
            for row in table.rows
            if best is not None and row.scores[index] is not None
            and row.scores[index] >= best
        )
        results.append(CourseworkTopScore(
            column.coursework_id, column.title, column.max_points, best, scorers))
    return tuple(results)


__all__ = [
    "MISSING_PENALTY_RATE", "CourseworkColumn", "CourseworkTopScore",
    "RankedStudent", "RankingTable", "TopScorer", "build_ranking", "top_scorers",
]
