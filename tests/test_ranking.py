import math

import pytest

from grader.ranking import build_ranking


def test_only_teacher_confirmed_or_human_scores_are_aggregated():
    table = build_ranking([
        {
            "coursework_id": "cw1", "title": "課題1",
            "rows": [
                {"student_id": "1", "name": "A", "source": "system", "mapped_score": 9},
                {"student_id": "2", "name": "B", "source": "human", "mapped_score": 7},
                {"student_id": "3", "name": "C", "source": "human", "mapped_score": 6, "category": "not_submitted"},
            ],
            "reviews": {
                "1": {"status": "confirmed", "score": 8},
                "2": {"status": "proposal", "score": 10},
                "3": {"status": "confirmed", "score": 6},
            },
        },
        {
            "coursework_id": "cw2", "title": "課題2",
            "rows": [
                {"student_id": "1", "name": "A", "source": "system", "mapped_score": 9},
                {"student_id": "2", "name": "B", "source": "human", "assigned_grade": 3},
                {"student_id": "3", "name": "C", "source": "system", "mapped_score": 5},
            ],
        },
    ])
    by_id = {row.student_id: row for row in table.rows}
    assert by_id["1"].scores == (8.0, None)
    assert by_id["1"].total == 8
    assert by_id["1"].confirmed_count == 1
    assert by_id["2"].scores == (7.0, 3.0)
    assert by_id["2"].total == 10
    assert by_id["3"].scores == (None, None)


def test_competition_rank_is_stable_for_ties_and_zero_scores():
    table = build_ranking([{
        "coursework_id": "cw",
        "rows": [
            {"student_id": "first", "name": "First", "source": "human", "mapped_score": 10},
            {"student_id": "second", "name": "Second", "source": "human", "mapped_score": 10},
            {"student_id": "third", "name": "Third", "source": "human", "mapped_score": 8},
            {"student_id": "unconfirmed", "name": "None", "source": "system", "mapped_score": 99},
        ],
    }])
    assert [(row.student_id, row.rank) for row in table.rows] == [
        ("first", 1), ("second", 1), ("third", 3), ("unconfirmed", 4),
    ]


def test_invalid_scores_and_duplicate_coursework_are_rejected_or_excluded():
    table = build_ranking([{"coursework_id": "cw", "rows": [
        {"student_id": "a", "source": "human", "mapped_score": math.nan},
        {"student_id": "b", "source": "human", "mapped_score": True},
        {"student_id": "c", "source": "human", "mapped_score": -1},
    ]}])
    assert all(row.confirmed_count == 0 for row in table.rows)
    with pytest.raises(ValueError):
        build_ranking([{"coursework_id": "x", "rows": []}, {"coursework_id": "x", "rows": []}])
