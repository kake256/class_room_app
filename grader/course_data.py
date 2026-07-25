"""コース別データの安全なパス解決。"""
from __future__ import annotations

import pathlib
import re
from dataclasses import dataclass

from .config import Config
from .fetch import normalize_gid


def is_human_protected(meta: dict) -> bool:
    """0点を含む人間成績またはRETURNEDを保護する。"""
    return (
        meta.get("assigned_grade") is not None
        or meta.get("draft_grade") is not None
        or meta.get("state") == "RETURNED"
    )


def numeric_id(value: str, label: str) -> str:
    normalized = normalize_gid(str(value))
    if not re.fullmatch(r"[0-9]+", normalized):
        raise ValueError(f"{label}が不正です")
    return normalized


@dataclass(frozen=True)
class CoursePaths:
    cfg: Config
    course_id: str
    coursework_id: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "course_id", numeric_id(self.course_id, "course_id"))
        object.__setattr__(self, "coursework_id", numeric_id(self.coursework_id, "coursework_id"))

    @property
    def root(self) -> pathlib.Path:
        return (self.cfg.data_dir / "courses" / self.course_id / "courseworks" /
                self.coursework_id)

    @property
    def settings(self) -> pathlib.Path:
        return self.root / "settings.json"

    @property
    def meta(self) -> pathlib.Path:
        return self.root / "meta.jsonl"

    @property
    def raw(self) -> pathlib.Path:
        return self.root / "raw"

    @property
    def pdf(self) -> pathlib.Path:
        return self.root / "pdf"

    @property
    def pages(self) -> pathlib.Path:
        return self.root / "pages"

    @property
    def results(self) -> pathlib.Path:
        return self.root / "results"

    @property
    def report(self) -> pathlib.Path:
        return self.root / "report.csv"

    def read_path(self, kind: str) -> pathlib.Path:
        scoped = getattr(self, kind)
        if scoped.exists():
            return scoped
        configured_course = numeric_id(
            str(self.cfg.get("classroom", "course_id", default="")), "course_id"
        ) if self.cfg.get("classroom", "course_id", default="") else None
        if configured_course != self.course_id:
            return scoped
        legacy = {
            "meta": self.cfg.data_dir / "meta" / f"{self.coursework_id}.jsonl",
            "raw": self.cfg.data_dir / "raw" / self.coursework_id,
            "pdf": self.cfg.data_dir / "pdf" / self.coursework_id,
            "pages": self.cfg.data_dir / "pages" / self.coursework_id,
            "results": self.cfg.data_dir / "results" / self.coursework_id,
            "report": self.cfg.data_dir / "report" / f"{self.coursework_id}.csv",
        }
        return legacy.get(kind, scoped)
