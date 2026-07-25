import pytest

from grader.ranking import build_ranking
from grader.sheets_export import ranking_to_sheet_values, sanitize_cell, validate_destination, write_values


class Call:
    def __init__(self, result=None):
        self.result = result or {}

    def execute(self):
        return self.result


class ValuesService:
    def __init__(self):
        self.calls = []

    def clear(self, **kwargs):
        self.calls.append(("clear", kwargs))
        return Call()

    def update(self, **kwargs):
        self.calls.append(("update", kwargs))
        return Call({"updatedRows": 2})


class SheetsService:
    def __init__(self):
        self.values_service = ValuesService()

    def spreadsheets(self):
        return self

    def values(self):
        return self.values_service


def test_values_have_required_columns_and_formula_strings_are_escaped():
    table = build_ranking([{"coursework_id": "cw", "title": "=課題", "rows": [
        {"student_id": "1", "name": "+SUM(A1:A2)", "source": "human", "mapped_score": 4},
    ]}])
    values = ranking_to_sheet_values(table)
    assert values[0] == ["順位", "氏名", "課題の平均点", "最高点回数", "提出数",
                         "未提出数", "確定点合計", "確定課題数", "'=課題"]
    # 満点不明のため受講者中の最高点で正規化され、平均点は1.0になる
    assert values[1] == [1, "'+SUM(A1:A2)", 1.0, 1, 1, 0, 4.0, 1, 4.0]
    assert sanitize_cell("@cmd") == "'@cmd"
    assert sanitize_cell("-1+2") == "'-1+2"


def test_writer_clears_then_updates_explicit_range_without_network():
    service = SheetsService()
    spreadsheet_id = "a_valid_spreadsheet_id_12345"
    result = write_values(spreadsheet_id, "'ランキング 1'!A1:F20", [["=x", 1]], service=service)
    assert result == {"updatedRows": 2}
    assert [name for name, _ in service.values_service.calls] == ["clear", "update"]
    update = service.values_service.calls[1][1]
    assert update["valueInputOption"] == "RAW"
    assert update["body"] == {"values": [["'=x", 1]]}


@pytest.mark.parametrize("spreadsheet_id", ["short", "bad/id", "x" * 129])
def test_invalid_spreadsheet_id_is_rejected_before_service_call(spreadsheet_id):
    service = SheetsService()
    with pytest.raises(ValueError):
        write_values(spreadsheet_id, "Sheet1!A1:B2", [[1]], service=service)
    assert service.values_service.calls == []


@pytest.mark.parametrize("a1", ["A1:B2", "Sheet1!A:B", "Sheet1!B2:A1", "Sheet1!A0:B2", "Sheet 1!A1:B2"])
def test_invalid_or_ambiguous_range_is_rejected(a1):
    with pytest.raises(ValueError):
        validate_destination("a_valid_spreadsheet_id_12345", a1)


def test_values_must_fit_explicit_range_and_credentials_are_not_implicit():
    with pytest.raises(ValueError):
        write_values("a_valid_spreadsheet_id_12345", "Sheet1!A1:B1", [[1, 2], [3, 4]], service=SheetsService())
    with pytest.raises(ValueError):
        write_values("a_valid_spreadsheet_id_12345", "Sheet1!A1:B2", [[1]])


def test_sheet_values_include_submission_summary_columns():
    """Sheets出力にも提出回数・最高点回数・未提出回数を含める。"""
    from grader.ranking import build_ranking

    table = build_ranking([
        {"coursework_id": "1", "title": "課題1", "max_points": 10, "rows": [
            {"student_id": "a", "name": "A", "source": "human", "mapped_score": 10},
            {"student_id": "b", "name": "B", "category": "not_submitted"},
        ]},
    ])
    values = ranking_to_sheet_values(table)
    assert values[0] == ["順位", "氏名", "課題の平均点", "最高点回数", "提出数",
                         "未提出数", "確定点合計", "確定課題数", "課題1 / 10点"]
    # A: 10/10 = 1.0、B: 未提出のみなので -1/3
    assert values[1] == [1, "A", 1.0, 1, 1, 0, 10, 1, 10]
    assert values[2][:8] == [2, "B", -1 / 3, 0, 0, 1, 0, 0]
    # 未提出セルは-1として出す
    assert values[2][8] == -1
