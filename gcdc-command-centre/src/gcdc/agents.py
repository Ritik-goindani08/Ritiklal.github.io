"""Agent run logging — feeds the Agent Health page.

Python agents:
    with agent_run(conn, "OUTREACH") as run:
        ...
        run.processed += 10; run.created += 3
        run.warn("2 contacts had no email")

Anything else (a Claude session, a PowerShell script):
    gcdc run start --agent OUTREACH            -> prints run_id
    gcdc run finish --run-id 42 --status SUCCESS --processed 10 --created 3
"""

from __future__ import annotations

import contextlib
import json
import socket
import sqlite3
import traceback
from dataclasses import dataclass, field
from typing import Iterator

from gcdc import __version__
from gcdc.ingest import raise_alert
from gcdc.timeutil import now_iso

RUN_STATUSES = ("SUCCESS", "WARNING", "FAILED", "ABORTED")


def ensure_agent(conn: sqlite3.Connection, agent_code: str) -> None:
    conn.execute("INSERT OR IGNORE INTO agents (agent_code, agent_name, updated_at) VALUES (?, ?, ?)",
                 (agent_code, agent_code.replace("_", " ").title(), now_iso()))


def start_run(conn: sqlite3.Connection, agent_code: str, *, trigger: str = "SCHEDULED",
              version: str | None = None) -> int:
    agent_code = agent_code.upper()
    ensure_agent(conn, agent_code)
    return conn.execute(
        "INSERT INTO agent_runs (agent_code, started_at, status, trigger_type, host, agent_version) "
        "VALUES (?, ?, 'RUNNING', ?, ?, ?)",
        (agent_code, now_iso(), trigger.upper(), socket.gethostname(), version or __version__)).lastrowid


def finish_run(conn: sqlite3.Connection, run_id: int, *, status: str, processed: int = 0, created: int = 0,
               updated: int = 0, skipped: int = 0, errors: int = 0, warnings: int = 0, last_error: str | None = None,
               next_scheduled_run: str | None = None, details: dict | None = None) -> None:
    status = status.upper()
    if status not in RUN_STATUSES:
        raise ValueError(f"status must be one of {RUN_STATUSES}")
    row = conn.execute("SELECT agent_code FROM agent_runs WHERE run_id = ?", (run_id,)).fetchone()
    if row is None:
        raise ValueError(f"run_id {run_id} does not exist")
    conn.execute(
        "UPDATE agent_runs SET finished_at = ?, status = ?, records_processed = ?, records_created = ?, "
        "records_updated = ?, records_skipped = ?, errors_count = ?, warnings_count = ?, last_error = ?, "
        "next_scheduled_run = ?, details_json = ? WHERE run_id = ?",
        (now_iso(), status, processed, created, updated, skipped, errors, warnings,
         (last_error or None) and last_error[:2000], next_scheduled_run,
         json.dumps(details, default=str) if details else None, run_id))
    agent = row[0]
    if status in ("FAILED", "ABORTED"):
        raise_alert(conn, key=f"agent-failed-{agent}", source_type="AGENT", severity="ERROR", category="AGENT_RUN",
                    message=f"{agent} run {run_id} {status.lower()}: {last_error or 'no error text'}",
                    entity_type="agent_runs", entity_id=run_id, agent_code=agent, run_id=run_id)
    elif status == "SUCCESS":
        conn.execute("UPDATE alerts SET status = 'RESOLVED', resolved_at = ?, resolved_by = 'auto' "
                     "WHERE alert_key = ? AND status <> 'RESOLVED'", (now_iso(), f"agent-failed-{agent}"))


@dataclass
class RunHandle:
    run_id: int
    agent_code: str
    processed: int = 0
    created: int = 0
    updated: int = 0
    skipped: int = 0
    errors: int = 0
    warnings: list[str] = field(default_factory=list)
    last_error: str | None = None
    next_scheduled_run: str | None = None
    details: dict = field(default_factory=dict)

    def warn(self, message: str) -> None:
        self.warnings.append(message)

    def error(self, message: str) -> None:
        self.errors += 1
        self.last_error = message


@contextlib.contextmanager
def agent_run(conn: sqlite3.Connection, agent_code: str, *, trigger: str = "SCHEDULED") -> Iterator[RunHandle]:
    handle = RunHandle(start_run(conn, agent_code, trigger=trigger), agent_code.upper())
    try:
        yield handle
    except BaseException as exc:
        finish_run(conn, handle.run_id, status="FAILED", processed=handle.processed, created=handle.created,
                   updated=handle.updated, skipped=handle.skipped, errors=handle.errors + 1,
                   warnings=len(handle.warnings), last_error=f"{type(exc).__name__}: {exc}",
                   details={"traceback": traceback.format_exc()[-4000:], **handle.details})
        raise
    status = "FAILED" if handle.errors and not (handle.created or handle.updated) else \
        "WARNING" if handle.errors or handle.warnings else "SUCCESS"
    if handle.warnings:
        handle.details.setdefault("warnings", handle.warnings[:50])
    finish_run(conn, handle.run_id, status=status, processed=handle.processed, created=handle.created,
               updated=handle.updated, skipped=handle.skipped, errors=handle.errors, warnings=len(handle.warnings),
               last_error=handle.last_error, next_scheduled_run=handle.next_scheduled_run,
               details=handle.details or None)
