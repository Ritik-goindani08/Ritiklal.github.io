"""Load config/gcdc.toml and resolve paths.

Resolution order for the config file: explicit path > $GCDC_CONFIG >
<project>/config/gcdc.toml. Paths inside the config are relative to the
project folder (the folder that contains config/).
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = PROJECT_ROOT / "config" / "gcdc.toml"


@dataclass
class Config:
    raw: dict[str, Any]
    path: Path
    root: Path = field(default=PROJECT_ROOT)

    def get(self, dotted: str, default: Any = None) -> Any:
        node: Any = self.raw
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def _path(self, value: str) -> Path:
        p = Path(value).expanduser()
        return p if p.is_absolute() else (self.root / p)

    @property
    def db_path(self) -> Path:
        env = os.environ.get("GCDC_DB")
        return self._path(env) if env else self._path(self.get("database.path", "data/gcdc.db"))

    @property
    def export_dir(self) -> Path:
        env = os.environ.get("GCDC_EXPORT_DIR")
        return self._path(env) if env else self._path(self.get("export.dir", "exports/powerbi"))

    @property
    def briefs_dir(self) -> Path:
        env = os.environ.get("GCDC_BRIEFS_DIR")
        return self._path(env) if env else self._path(self.get("export.briefs_dir", "exports/briefs"))

    @property
    def utc_offset_hours(self) -> float:
        return float(self.get("business.utc_offset_hours", 10))

    @property
    def free_mail_domains(self) -> frozenset[str]:
        return frozenset(d.lower() for d in self.get("outreach.free_mail_domains", []))

    @property
    def public_holidays(self) -> frozenset[str]:
        return frozenset(self.get("calendar.public_holidays", []))


def load_config(path: str | os.PathLike[str] | None = None) -> Config:
    chosen = Path(path) if path else Path(os.environ.get("GCDC_CONFIG", DEFAULT_CONFIG))
    with open(chosen, "rb") as fh:
        raw = tomllib.load(fh)
    root = chosen.resolve().parent.parent if chosen.resolve().parent.name == "config" else PROJECT_ROOT
    return Config(raw=raw, path=chosen.resolve(), root=root)
