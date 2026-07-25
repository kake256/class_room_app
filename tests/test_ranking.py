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


def test_ranking_counts_submissions_top_scores_and_missing():
    """提出回数・最高点回数・未提出回数を集計する。"""
    table = build_ranking([
        {"coursework_id": "1", "title": "課題1", "rows": [
            {"student_id": "a", "name": "A", "source": "human", "mapped_score": 10},
            {"student_id": "b", "name": "B", "source": "human", "mapped_score": 8},
            {"student_id": "c", "name": "C", "category": "not_submitted"},
        ]},
        {"coursework_id": "2", "title": "課題2", "rows": [
            {"student_id": "a", "name": "A", "source": "human", "mapped_score": 5},
            {"student_id": "b", "name": "B", "source": "human", "mapped_score": 9},
            {"student_id": "c", "name": "C", "state": "NOT_SUBMITTED"},
        ]},
    ])
    by_id = {row.student_id: row for row in table.rows}
    # Aは課題1で最高点、Bは課題2で最高点
    assert by_id["a"].top_score_count == 1 and by_id["b"].top_score_count == 1
    assert by_id["a"].submitted_count == 2 and by_id["a"].not_submitted_count == 0
    # Cは両方とも未提出。提出回数0・確定0で最下位
    assert by_id["c"].submitted_count == 0 and by_id["c"].not_submitted_count == 2
    assert by_id["c"].confirmed_count == 0 and by_id["c"].total == 0
    # 合計は変わらない(A=15, B=17)
    assert by_id["a"].total == 15 and by_id["b"].total == 17


def test_ranking_top_score_counts_ties_for_every_holder():
    """同点で最高点なら双方を最高点回数に数える。"""
    table = build_ranking([
        {"coursework_id": "1", "title": "課題1", "rows": [
            {"student_id": "a", "name": "A", "source": "human", "mapped_score": 7},
            {"student_id": "b", "name": "B", "source": "human", "mapped_score": 7},
        ]},
    ])
    assert all(row.top_score_count == 1 for row in table.rows)


def test_missing_penalty_and_normalized_average_decide_the_rank():
    """未提出はペナルティ、順位は満点で正規化した平均点で決まる。"""
    from grader.ranking import MISSING_PENALTY_RATE

    table = build_ranking([
        {"coursework_id": "1", "title": "小課題", "max_points": 3, "rows": [
            {"student_id": "a", "name": "A", "source": "human", "mapped_score": 3},
            {"student_id": "b", "name": "B", "source": "human", "mapped_score": 3},
        ]},
        {"coursework_id": "2", "title": "大課題", "max_points": 100, "rows": [
            {"student_id": "a", "name": "A", "source": "human", "mapped_score": 50},
            {"student_id": "b", "name": "B", "category": "not_submitted"},
        ]},
    ])
    by_id = {row.student_id: row for row in table.rows}
    # 満点が違っても比率で扱う: A = (3/3 + 50/100) / 2 = 0.75
    assert by_id["a"].average_rate == 0.75
    # Bは未提出でペナルティ: (3/3 + (-1/3)) / 2
    assert by_id["b"].average_rate == (1 + MISSING_PENALTY_RATE) / 2
    assert by_id["a"].rank == 1 and by_id["b"].rank == 2
    # 確定点合計だけならB(3点)よりA(53点)が上だが、順位は比率で決まる
    assert by_id["a"].total == 53 and by_id["b"].total == 3
    # 未提出は列ごとに識別できる(未確定と区別して表示するため)
    assert by_id["b"].not_submitted_flags == (False, True)


def test_unconfirmed_answers_are_excluded_from_the_average_denominator():
    """提出済みで未確定の課題は平均点の母数に入れない(不当な減点を避ける)。"""
    table = build_ranking([
        {"coursework_id": "1", "title": "確定済み", "max_points": 10, "rows": [
            {"student_id": "a", "name": "A", "source": "human", "mapped_score": 8},
        ]},
        {"coursework_id": "2", "title": "未確定", "max_points": 10, "rows": [
            {"student_id": "a", "name": "A", "source": "system", "mapped_score": 10},
        ]},
    ])
    row = table.rows[0]
    assert row.average_rate == 0.8      # 未確定を0点扱いにしない
    assert row.evaluated_count == 1 and row.unconfirmed_count == 1
    assert row.not_submitted_count == 0


def test_ties_are_broken_by_top_score_count():
    """平均点が同率なら最高点回数の多い方を上位にする。"""
    table = build_ranking([
        {"coursework_id": "1", "title": "課題1", "max_points": 10, "rows": [
            {"student_id": "a", "name": "A", "source": "human", "mapped_score": 10},
            {"student_id": "b", "name": "B", "source": "human", "mapped_score": 6},
        ]},
        {"coursework_id": "2", "title": "課題2", "max_points": 10, "rows": [
            {"student_id": "a", "name": "A", "source": "human", "mapped_score": 6},
            {"student_id": "b", "name": "B", "source": "human", "mapped_score": 10},
        ]},
    ])
    # 平均は同じ0.8だが、同点最高点なので両者とも最高点回数2で同率1位
    assert [(row.student_id, row.rank) for row in table.rows] == [("a", 1), ("b", 1)]
    assert all(row.average_rate == 0.8 for row in table.rows)
