"""Database connection, migrations, repeatable views and config sync."""

from __future__ import annotations

import contextlib
import json
import sqlite3
from datetime import datetime
from importlib import resources
from pathlib import Path
from typing import Iterator

from gcdc.config import Config
from gcdc.timeutil import local_today, now_iso, to_iso_utc

MIGRATIONS_PKG = "gcdc.sql.migrations"
VIEWS_PKG = "gcdc.sql.views"


def connect(path: str | Path) -> sqlite3.Connection:
    path = Path(path)
    if str(path) != ":memory:":
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    if str(path) != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL")
    return conn


@contextlib.contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Explicit transaction (connections run in autocommit mode otherwise)."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def _sql_files(package: str) -> list[tuple[str, str]]:
    files = [f for f in resources.files(package).iterdir() if f.name.endswith(".sql")]
    return [(f.name, f.read_text(encoding="utf-8")) for f in sorted(files, key=lambda f: f.name)]


def migrate(conn: sqlite3.Connection) -> list[str]:
    """Apply pending migrations, then rebuild every view. Returns applied names."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations (name TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
    )
    done = {r[0] for r in conn.execute("SELECT name FROM schema_migrations")}
    applied = []
    for name, sql in _sql_files(MIGRATIONS_PKG):
        if name in done:
            continue
        conn.execute("BEGIN IMMEDIATE")
        try:
            # executescript would COMMIT our transaction; run statements one by one.
            for stmt in _split_sql(sql):
                conn.execute(stmt)
            conn.execute("INSERT INTO schema_migrations VALUES (?, ?)", (name, now_iso()))
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")
        applied.append(name)
    rebuild_views(conn)
    return applied


def rebuild_views(conn: sqlite3.Connection) -> None:
    with transaction(conn):
        for (view,) in conn.execute("SELECT name FROM sqlite_master WHERE type = 'view'").fetchall():
            conn.execute(f'DROP VIEW IF EXISTS "{view}"')
        for _name, sql in _sql_files(VIEWS_PKG):
            for stmt in _split_sql(sql):
                conn.execute(stmt)


def _split_sql(script: str) -> list[str]:
    """Split a script into statements, keeping CREATE TRIGGER ... END blocks whole."""
    statements, buf = [], []
    for line in script.splitlines():
        buf.append(line)
        candidate = "\n".join(buf).strip()
        if candidate and sqlite3.complete_statement(candidate):
            body = "\n".join(l for l in buf if not l.strip().startswith("--")).strip()
            if body:
                statements.append(candidate)
            buf = []
    tail = "\n".join(l for l in buf if not l.strip().startswith("--")).strip()
    if tail:
        raise ValueError(f"incomplete SQL statement at end of script: {tail[:80]}")
    return statements


def backup(conn: sqlite3.Connection, dest_dir: Path, *, keep: int = 30, stamp: str | None = None) -> Path:
    """Consistent online copy (SQLite backup API) to dest_dir/gcdc-YYYY-MM-DD.db; keeps the newest `keep`.

    The live database must not sit in a OneDrive/Dropbox-synced folder, but these
    backup files can, which is the simplest off-machine copy.
    """
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    target = dest_dir / f"gcdc-{stamp or datetime.now().strftime('%Y-%m-%d')}.db"
    tmp = target.with_suffix(".db.tmp")
    out = sqlite3.connect(tmp)
    try:
        conn.backup(out)
    finally:
        out.close()
    tmp.replace(target)
    for old in sorted(dest_dir.glob("gcdc-*.db"))[:-keep] if keep > 0 else []:
        old.unlink()
    return target


def open_db(cfg: Config) -> sqlite3.Connection:
    conn = connect(cfg.db_path)
    migrate(conn)
    return conn


# ---------------------------------------------------------------------------
# Runtime "as of" pinning — every view reads v_ctx for "now" / "today".
# ---------------------------------------------------------------------------
@contextlib.contextmanager
def pinned_now(conn: sqlite3.Connection, at: datetime | str | None, tz_offset_hours: float) -> Iterator[str]:
    stamp = at if isinstance(at, str) else to_iso_utc(at) if at else now_iso()
    previous = tuple(conn.execute("SELECT as_of_utc, pinned_at FROM runtime_context WHERE id = 1").fetchone())
    conn.execute("UPDATE runtime_context SET as_of_utc = ?, pinned_at = ?, tz_offset_hours = ? WHERE id = 1",
                 (stamp, now_iso(), tz_offset_hours))
    try:
        yield stamp
    finally:
        # Restore rather than clear, so nested pins (snapshot inside a refresh) are safe.
        conn.execute("UPDATE runtime_context SET as_of_utc = ?, pinned_at = ? WHERE id = 1", previous)


# ---------------------------------------------------------------------------
# Config -> database
# ---------------------------------------------------------------------------
_TARGETS = {
    "mrr": ("MRR", "AUD", "Monthly recurring revenue target"),
    "weekly_hours_min": ("WEEKLY_HOURS_MIN", "hours", "Recurring weekly billable hours — lower target"),
    "weekly_hours_max": ("WEEKLY_HOURS_MAX", "hours", "Recurring weekly billable hours — upper target"),
}


def sync_config(conn: sqlite3.Connection, cfg: Config) -> dict[str, int]:
    """Push gcdc.toml into settings, versioned targets, stage rules and agents."""
    now = now_iso()
    today = local_today(cfg.utc_offset_hours).isoformat()
    changed_targets = 0
    with transaction(conn):
        conn.execute("UPDATE runtime_context SET tz_offset_hours = ? WHERE id = 1", (cfg.utc_offset_hours,))

        flat = _flatten({k: v for k, v in cfg.raw.items() if k not in ("agents",)})
        for key, value in flat.items():
            vtype = ("bool" if isinstance(value, bool) else "number" if isinstance(value, (int, float))
                     else "json" if isinstance(value, (list, dict)) else "text")
            stored = json.dumps(value) if vtype == "json" else (str(int(value)) if vtype == "bool" else str(value))
            conn.execute(
                "INSERT INTO settings (key, value, value_type, updated_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value, value_type = excluded.value_type, "
                "updated_at = excluded.updated_at WHERE settings.value IS NOT excluded.value",
                (key, stored, vtype, now))

        for key, (code, unit, desc) in _TARGETS.items():
            value = cfg.get(f"targets.{key}")
            if value is None:
                continue
            current = conn.execute(
                "SELECT target_value FROM business_targets WHERE target_code = ? "
                "ORDER BY effective_from DESC LIMIT 1", (code,)).fetchone()
            if current is None or float(current[0]) != float(value):
                # The first version applies from the start of history; later edits from today.
                effective = "2000-01-01" if current is None else today
                conn.execute(
                    "INSERT INTO business_targets (target_code, target_value, unit, description, effective_from, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(target_code, effective_from) DO UPDATE SET "
                    "target_value = excluded.target_value, created_at = excluded.created_at",
                    (code, float(value), unit, desc, effective, now))
                changed_targets += 1

        use_probs = bool(cfg.get("pipeline.use_stage_probabilities", True))
        probs = cfg.get("pipeline.stage_probability", {}) or {}
        for (stage,) in conn.execute("SELECT stage_code FROM ref_pipeline_stage").fetchall():
            p = probs.get(stage) if use_probs else None
            conn.execute("UPDATE ref_pipeline_stage SET default_probability = ? WHERE stage_code = ?",
                         (None if p is None else float(p), stage))

        for code, spec in (cfg.get("agents", {}) or {}).items():
            conn.execute(
                "INSERT INTO agents (agent_code, agent_name, schedule_description, expected_interval_hours, "
                "business_days_only, is_active, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(agent_code) DO UPDATE SET schedule_description = excluded.schedule_description, "
                "expected_interval_hours = excluded.expected_interval_hours, "
                "business_days_only = excluded.business_days_only, is_active = excluded.is_active, "
                "updated_at = excluded.updated_at",
                (code, spec.get("name", code.replace("_", " ").title()), spec.get("schedule"),
                 spec.get("interval_hours"), int(bool(spec.get("business_days_only", False))),
                 int(bool(spec.get("active", True))), now))
    return {"targets_changed": changed_targets, "settings": len(flat)}


def _flatten(d: dict, prefix: str = "") -> dict:
    out = {}
    for k, v in d.items():
        key = f"{prefix}{k}"
        if isinstance(v, dict):
            out.update(_flatten(v, key + "."))
        else:
            out[key] = v
    return out
