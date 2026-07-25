"""Course- or owner-scoped grading-settings templates.

The store performs no logging. Owner identifiers are hashed before becoming path
components, and all records for one scope are kept in one private atomic JSON file.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import pathlib
import re
import secrets
import tempfile
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Mapping

from .config import Config
from .course_settings import validate_settings


MAX_TEMPLATES = 50
MAX_NAME = 80
_TEMPLATE_ID = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
_COURSE_ID = re.compile(r"^[0-9]{1,128}$")


@dataclass(frozen=True)
class TemplateScope:
    """An explicit isolation boundary; never construct paths from raw input."""

    kind: str
    key: str

    @classmethod
    def course(cls, course_id: str) -> "TemplateScope":
        value = str(course_id)
        if not _COURSE_ID.fullmatch(value):
            raise ValueError("invalid course_id")
        return cls("course", value)

    @classmethod
    def owner(cls, owner_id: str) -> "TemplateScope":
        value = str(owner_id)
        if not value or len(value) > 1024:
            raise ValueError("invalid owner identifier")
        return cls("owner", hashlib.sha256(value.encode("utf-8")).hexdigest())

    def __post_init__(self) -> None:
        valid = (
            self.kind == "course" and _COURSE_ID.fullmatch(self.key)
            or self.kind == "owner" and re.fullmatch(r"[0-9a-f]{64}", self.key)
        )
        if not valid:
            raise ValueError("invalid template scope")


class TemplateStoreError(RuntimeError):
    pass


def _finite_positive(value: Any, label: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{label} is invalid") from None
    if not math.isfinite(number) or number <= 0:
        raise ValueError(f"{label} must be finite and positive")
    return number


def _template_name(value: Any) -> str:
    name = str(value or "").strip()
    if not name or len(name) > MAX_NAME or any(ord(char) < 32 for char in name):
        raise ValueError(f"template name must be 1-{MAX_NAME} printable characters")
    return name


def _timestamp(clock: Callable[[], float]) -> str:
    return datetime.fromtimestamp(clock(), tz=timezone.utc).isoformat(timespec="seconds")


def _normalize(name: str, settings: Mapping[str, Any], source_max_points: float) -> dict[str, Any]:
    maximum = _finite_positive(source_max_points, "source_max_points")
    clean = validate_settings(dict(settings), maximum)
    mapping: dict[str, float] = {}
    for score in range(4):
        ratio = float(clean["score_mapping"][str(score)]) / maximum
        if not math.isfinite(ratio):
            raise ValueError("score mapping must be finite")
        mapping[str(score)] = ratio
    penalty = float(clean["late_penalty"]) / maximum
    if not math.isfinite(penalty):
        raise ValueError("late penalty must be finite")
    return {
        "name": _template_name(name),
        "notes": clean["notes"],
        "levels": clean["levels"],
        "score_mapping": mapping,
        "late_penalty": penalty,
        "mapping_mode": "ratio",
    }


def _atomic_write(path: pathlib.Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


class SettingsTemplateStore:
    """CRUD and application service for one explicit course/owner scope."""

    def __init__(
        self, cfg: Config, scope: TemplateScope, *,
        clock: Callable[[], float] | None = None,
        id_factory: Callable[[], str] | None = None,
    ):
        self.scope = scope
        self.path = cfg.data_dir / "settings_templates" / scope.kind / f"{scope.key}.json"
        self.clock = clock or (lambda: datetime.now(tz=timezone.utc).timestamp())
        self.id_factory = id_factory or (lambda: secrets.token_urlsafe(18))
        self.lock = threading.RLock()

    def _load(self) -> dict[str, dict[str, Any]]:
        if not self.path.exists():
            return {}
        try:
            document = json.loads(self.path.read_text(encoding="utf-8"))
            templates = document["templates"]
            if document.get("version") != 1 or not isinstance(templates, dict):
                raise ValueError
        except (OSError, KeyError, ValueError, json.JSONDecodeError) as exc:
            raise TemplateStoreError("template store cannot be read") from exc
        return templates

    def _save(self, templates: dict[str, dict[str, Any]]) -> None:
        _atomic_write(self.path, {"version": 1, "templates": templates})

    @staticmethod
    def _id(template_id: str) -> str:
        value = str(template_id)
        if not _TEMPLATE_ID.fullmatch(value):
            raise ValueError("invalid template_id")
        return value

    def create(
        self, *, name: str, settings: Mapping[str, Any], source_max_points: float,
    ) -> dict[str, Any]:
        value = _normalize(name, settings, source_max_points)
        with self.lock:
            templates = self._load()
            if len(templates) >= MAX_TEMPLATES:
                raise ValueError(f"a scope may contain at most {MAX_TEMPLATES} templates")
            template_id = self._id(self.id_factory())
            if template_id in templates:
                raise TemplateStoreError("template id collision")
            value.update(id=template_id, updated_at=_timestamp(self.clock))
            templates[template_id] = value
            self._save(templates)
            return dict(value)

    def list(self) -> list[dict[str, Any]]:
        with self.lock:
            templates = self._load()
        return [dict(value) for _, value in sorted(
            templates.items(), key=lambda item: (item[1].get("name", ""), item[0]))
        ]

    def get(self, template_id: str) -> dict[str, Any] | None:
        key = self._id(template_id)
        with self.lock:
            value = self._load().get(key)
        return dict(value) if value else None

    def update(
        self, template_id: str, *, name: str, settings: Mapping[str, Any],
        source_max_points: float,
    ) -> dict[str, Any]:
        key = self._id(template_id)
        value = _normalize(name, settings, source_max_points)
        with self.lock:
            templates = self._load()
            if key not in templates:
                raise KeyError(key)
            value.update(id=key, updated_at=_timestamp(self.clock))
            templates[key] = value
            self._save(templates)
        return dict(value)

    def delete(self, template_id: str) -> bool:
        key = self._id(template_id)
        with self.lock:
            templates = self._load()
            if key not in templates:
                return False
            del templates[key]
            self._save(templates)
        return True

    def apply(
        self, template_id: str, *, max_points: float, confirmed: bool = False,
    ) -> dict[str, Any]:
        """Materialize ratios for a target assignment and validate its settings."""
        maximum = _finite_positive(max_points, "max_points")
        template = self.get(template_id)
        if template is None:
            raise KeyError(template_id)
        if template.get("mapping_mode") != "ratio":
            raise TemplateStoreError("unsupported template mapping mode")
        candidate = {
            "notes": template["notes"],
            "levels": template["levels"],
            "score_mapping": {
                str(score): round(float(template["score_mapping"][str(score)]) * maximum, 10)
                for score in range(4)
            },
            "late_penalty": round(float(template["late_penalty"]) * maximum, 10),
            "confirmed": bool(confirmed),
        }
        return validate_settings(candidate, maximum)


__all__ = ["SettingsTemplateStore", "TemplateScope", "TemplateStoreError"]
