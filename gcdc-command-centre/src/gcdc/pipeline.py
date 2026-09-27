"""The refresh pipeline: config sync -> reconcile -> data quality -> snapshots -> export.

Run on a schedule (see scripts/). Entirely deterministic — no AI model moves
or transforms any row. Logged as agent REFRESH_PIPELINE so its own health
shows on the Agent Health page.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from gcdc.agents import agent_run
from gcdc.config import Config
from gcdc.db import pinned_now, sync_config
from gcdc.export import export_all
from gcdc.quality import run_checks
from gcdc.reconcile import reconcile
from gcdc.snapshot import snapshot_catch_up


def refresh(conn: sqlite3.Connection, cfg: Config, *, export_dir: Path | None = None, snapshots: bool = True,
            trigger: str = "SCHEDULED") -> dict:
    summary: dict = {}
    with agent_run(conn, "REFRESH_PIPELINE", trigger=trigger) as run:
        summary["config"] = sync_config(conn, cfg)
        with pinned_now(conn, None, cfg.utc_offset_hours) as stamp:
            summary["as_of_utc"] = stamp
            summary["reconcile"] = reconcile(conn)
            summary["data_quality"] = run_checks(conn)
            if snapshots:
                summary["snapshots_written"] = snapshot_catch_up(conn, offset_hours=cfg.utc_offset_hours)
            summary["export"] = export_all(conn, cfg, export_dir, at=stamp)
        run.processed = sum(summary["export"]["tables"].values())
        run.updated = sum(summary["reconcile"].values())
        run.created = len(summary.get("snapshots_written", []))
        dq = summary["data_quality"]
        if dq["errors"]:
            run.warn(f"{dq['errors']} data-quality errors")
        run.details = {"reconcile": summary["reconcile"], "dq": dq["by_check"],
                       "export_dir": summary["export"]["dir"]}
    return summary
