"""Google Sheets value generation and narrowly-scoped range replacement."""
from __future__ import annotations

import re
from typing import Any, Sequence

from .ranking import RankingTable


_SPREADSHEET_ID = re.compile(r"^[A-Za-z0-9_-]{20,128}$")
_A1_RANGE = re.compile(
    r"^(?P<sheet>[A-Za-z0-9_]+|'(?:[^']|'')+')!"
    r"(?P<start_col>[A-Z]{1,3})(?P<start_row>[1-9][0-9]*):"
    r"(?P<end_col>[A-Z]{1,3})(?P<end_row>[1-9][0-9]*)$"
)
_FORMULA_PREFIXES = ("=", "+", "-", "@")


def sanitize_cell(value: Any) -> Any:
    """Neutralize strings that Google Sheets could interpret as formulas."""
    if isinstance(value, str) and value.startswith(_FORMULA_PREFIXES):
        return "'" + value
    return value


def ranking_to_sheet_values(table: RankingTable) -> list[list[Any]]:
    """Build sanitized two-dimensional Sheets values from a ranking table."""
    header: list[Any] = ["順位", "氏名", "確定点合計", "確定課題数"]
    header.extend(column.title for column in table.courseworks)
    values: list[list[Any]] = [header]
    for row in table.rows:
        values.append([row.rank, row.name, row.total, row.confirmed_count, *row.scores])
    return [[sanitize_cell(cell) for cell in row] for row in values]


def _column_number(column: str) -> int:
    value = 0
    for char in column:
        value = value * 26 + ord(char) - ord("A") + 1
    return value


def validate_destination(spreadsheet_id: str, a1_range: str) -> tuple[int, int]:
    """Validate IDs and an explicit, bounded ``Sheet!A1:Z99`` destination."""
    if not _SPREADSHEET_ID.fullmatch(spreadsheet_id):
        raise ValueError("invalid spreadsheet_id")
    match = _A1_RANGE.fullmatch(a1_range)
    if not match:
        raise ValueError("range must include a sheet and an explicit A1 rectangle")
    start_col, end_col = _column_number(match["start_col"]), _column_number(match["end_col"])
    start_row, end_row = int(match["start_row"]), int(match["end_row"])
    if start_col > end_col or start_row > end_row:
        raise ValueError("A1 range is reversed")
    rows, columns = end_row - start_row + 1, end_col - start_col + 1
    if rows > 100_000 or columns > 256:
        raise ValueError("A1 range is too large")
    return rows, columns


def write_values(
    spreadsheet_id: str,
    a1_range: str,
    values: Sequence[Sequence[Any]],
    *,
    credentials: Any = None,
    service: Any = None,
) -> dict[str, Any]:
    """Clear and replace one explicit Sheets range using RAW values.

    Callers may inject an authenticated Sheets ``service`` (preferred in tests)
    or Google ``credentials``.  This function never obtains credentials itself.
    """
    max_rows, max_columns = validate_destination(spreadsheet_id, a1_range)
    materialized = [[sanitize_cell(cell) for cell in row] for row in values]
    if len(materialized) > max_rows or any(len(row) > max_columns for row in materialized):
        raise ValueError("values exceed the explicit destination range")
    if service is None:
        if credentials is None:
            raise ValueError("credentials or service is required")
        from googleapiclient.discovery import build
        service = build("sheets", "v4", credentials=credentials, cache_discovery=False)
    ranges = service.spreadsheets().values()
    ranges.clear(spreadsheetId=spreadsheet_id, range=a1_range, body={}).execute()
    result = ranges.update(
        spreadsheetId=spreadsheet_id,
        range=a1_range,
        valueInputOption="RAW",
        body={"values": materialized},
    ).execute()
    return result if isinstance(result, dict) else {}


__all__ = [
    "ranking_to_sheet_values", "sanitize_cell", "validate_destination", "write_values",
]
