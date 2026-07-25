"""Compose courseworks, settings and readiness for one bulk API response.

This module performs no external API calls and no logging.  The caller remains
responsible for authenticating the user, checking course membership, and obtaining
the Google Classroom coursework list before calling :func:`compose_course_overview`.
"""
from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from .config import Config


_NUMERIC_ID = re.compile(r"^[0-9]+$")
_COURSEWORK_FIELDS = ("id", "title", "due_date", "max_points", "assignment_key")
_READINESS_FIELDS = (
    "ready", "total", "human_graded", "system_graded", "missing", "system_target",
    "report_exists", "meta_synced_at", "meta_stale", "meta_age_seconds",
)

SettingsLoader = Callable[[Config, str, str], Mapping[str, Any] | None]
ReadinessLoader = Callable[[Config, str, str], Mapping[str, Any]]


def _numeric_id(value: Any, label: str) -> str:
    normalized = str(value or "")
    if not _NUMERIC_ID.fullmatch(normalized):
        raise ValueError(f"invalid {label}")
    return normalized


def _load_settings(cfg: Config, course_id: str, coursework_id: str) -> Mapping[str, Any] | None:
    from .course_settings import load_settings
    return load_settings(cfg, course_id, coursework_id)


def _load_readiness(cfg: Config, course_id: str, coursework_id: str) -> Mapping[str, Any]:
    from .report import report_readiness
    return report_readiness(cfg, coursework_id, course_id=course_id)


def _public_readiness(value: Mapping[str, Any]) -> dict[str, Any]:
    """Return counts/status only; discard any accidental row-level additions."""
    return {key: value.get(key) for key in _READINESS_FIELDS}


def compose_course_overview(
    cfg: Config,
    course_id: str,
    courseworks: Sequence[Mapping[str, Any]],
    *,
    assignments: Mapping[str, Any] | None = None,
    settings_loader: SettingsLoader | None = None,
    readiness_loader: ReadinessLoader | None = None,
) -> dict[str, Any]:
    """Build an ordered, PII-free bulk course overview.

    Each dependency is invoked at most once for each unique coursework. Failures are
    isolated to that coursework and represented by stable error codes; exception
    messages are never returned. Duplicate or malformed coursework ids are rejected
    before local data is read.
    """
    selected = _numeric_id(course_id, "course_id")
    if not isinstance(courseworks, Sequence) or isinstance(courseworks, (str, bytes)):
        raise ValueError("courseworks must be a sequence")
    configured_assignments = assignments if assignments is not None else (
        cfg.get("assignments", default={}) or {}
    )
    if not isinstance(configured_assignments, Mapping):
        raise ValueError("assignments must be a mapping")
    get_settings = settings_loader or _load_settings
    get_readiness = readiness_loader or _load_readiness
    seen: set[str] = set()
    validated: list[tuple[str, Mapping[str, Any]]] = []
    for raw in courseworks:
        if not isinstance(raw, Mapping):
            raise ValueError("each coursework must be a mapping")
        coursework_id = _numeric_id(raw.get("id"), "coursework_id")
        if coursework_id in seen:
            raise ValueError("duplicate coursework_id")
        seen.add(coursework_id)
        validated.append((coursework_id, raw))

    overview: list[dict[str, Any]] = []
    for coursework_id, raw in validated:
        row = {field: raw.get(field) for field in _COURSEWORK_FIELDS}
        row["id"] = coursework_id
        if not row.get("assignment_key"):
            row["assignment_key"] = configured_assignments.get(coursework_id)
        errors: list[dict[str, str]] = []

        try:
            loaded_settings = get_settings(cfg, selected, coursework_id)
            if loaded_settings is not None and not isinstance(loaded_settings, Mapping):
                raise ValueError("invalid settings result")
            settings = dict(loaded_settings) if loaded_settings is not None else None
        except Exception:  # noqa: BLE001 isolate corrupt/unreadable coursework data
            settings = None
            errors.append({"code": "settings_unavailable"})

        try:
            loaded_readiness = get_readiness(cfg, selected, coursework_id)
            if not isinstance(loaded_readiness, Mapping):
                raise ValueError("invalid readiness result")
            readiness = _public_readiness(loaded_readiness)
        except Exception:  # noqa: BLE001 do not disclose paths, student data, or exception text
            readiness = None
            errors.append({"code": "readiness_unavailable"})

        # Preserve the current UI meaning: an existing but unconfirmed settings file
        # takes precedence over the legacy assignment mapping.
        row.update(
            settings=settings,
            readiness=readiness,
            configured=bool(settings.get("confirmed")) if settings is not None
            else bool(row.get("assignment_key")),
            overview_errors=errors,
        )
        overview.append(row)

    return {"course_id": selected, "courseworks": overview}


__all__ = ["compose_course_overview", "ReadinessLoader", "SettingsLoader"]
