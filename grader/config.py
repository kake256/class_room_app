"""config.yaml の読み込み。"""
from __future__ import annotations

import pathlib
from dataclasses import dataclass, field
from typing import Any

import yaml


@dataclass
class Config:
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def load(cls, path: str | pathlib.Path = "config.yaml") -> "Config":
        with open(path, encoding="utf-8") as f:
            return cls(raw=yaml.safe_load(f) or {})

    def get(self, *keys: str, default: Any = None) -> Any:
        cur: Any = self.raw
        for k in keys:
            if not isinstance(cur, dict) or k not in cur:
                return default
            cur = cur[k]
        return cur

    @property
    def data_dir(self) -> pathlib.Path:
        return pathlib.Path(self.get("paths", "data_dir", default="data"))
