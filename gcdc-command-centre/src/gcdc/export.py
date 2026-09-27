"""Export the Power BI-ready views to CSV.

Each pbi_* view becomes <export dir>/<view>.csv; a date dimension and the
data-quality check catalogue are generated alongside, plus manifest.json.
Files are written to a temp name and atomically replaced, so Power BI never
reads a half-written file. The export folder is output only — never edit it;
the database is the source of truth.

Column types are declared once here (EXPORT_TYPES) and reused by
powerbi/build_pbip.py, so the CSVs and the semantic model cannot drift apart.
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import sqlite3
from datetime import date, timedelta
from pathlib import Path

from gcdc import __version__
from gcdc.config import Config
from gcdc.db import pinned_now
from gcdc.quality import CHECKS
from gcdc.timeutil import now_iso

# Power BI types: int64 | double | string | dateTime (date-only columns use dateTime + date format)
_INT_PATTERNS = re.compile(
    r"(_id$|^id$|_order$|_sort$|^sort_order$|^tier$|^priority_rank$|^is_|^has_|^reached_|^found_|^counts_|"
    r"^includes_|^needs_|^cap_|_count$|^records_|^errors_count$|^warnings_count$|^occurrences$|_days$|^days_|"
    r"^day_of_week$|^touches_sent$|^requires_|^business_days_only$|^active_participants$|^active_rosters$|"
    r"^scheduled_rosters$|^active_opportunities$|^p[123]_|^opportunities_|^emails_|^followups_|^replies|"
    r"^approvals_|^meetings_|^referrals_|^applications_|^bounces_|^agents_|^dq_|^action_items|^orgs_|"
    r"^workers_|^open_worker|^unfilled_|^revenue_records$|^near_term_count$|^longer_term_count$|"
    r"^positive_conversations|^trial_shifts|^documents_|^partnerships_waiting$|^iso_week$|^year$|^month_number$|"
    r"^mrr_rosters|^pipeline_value_unknown|^near_term_unknown|^longer_term_unknown|^first_touch_sent$|"
    r"^positive_replies$|^emails_bounced$|^opportunities$|^organisations$|^outreach_records$|^age_days$|"
    r"^sequence_step$|^relative_day$|^checks_run$|^runtime_seconds$|^last_runtime_seconds$|^opportunities_reached$|"
    r"^is_catch_up$|^overdue_followups$|^open_worker_requirements$|^stage_order_reached$|^duration_minutes$)")
_DATE_PATTERNS = re.compile(
    r"(_date$|^date$|^effective_from$|^effective_to$|_expiry$|^next_action_due$|^start_date$|^end_date$|"
    r"^service_month$|^week_start$|^month_start$|^needed_by$|^as_of_date$)")
_DATETIME_PATTERNS = re.compile(
    r"(_local$|^last_successful_run$|^last_attempted_run$|^next_scheduled_run$|^last_error_at$)")
_STRING_OVERRIDES = {
    "scheduled_time", "meeting_time", "start_time", "end_time", "invoice_number", "abn", "postcode", "phone",
    "as_of_utc", "captured_at_utc", "generated_at", "last_dq_run_utc", "schema_version", "last_snapshot_date",
    "est_monthly_value_display", "potential_revenue_display", "year_month", "financial_year", "value_status",
    "brief_markdown", "brief_date",
}


# Columns whose names would otherwise be misread by the rules below.
_EXPLICIT_TYPES = {
    "actual_revenue_to_date": "double", "active_rate_not_approved": "int64", "is_estimated_date": "int64",
    "rag_sort": "int64", "severity_sort": "int64", "priority_sort": "int64", "stage_probability": "double",
    "default_probability": "double", "tier": "int64", "score": "double", "utilisation": "double",
    "hours_since_success": "double", "expected_interval_hours": "double", "max_weekly_hours": "double",
    "conversion_from_previous": "double", "mrr_pct_of_target": "double", "reply_rate": "double",
    "positive_reply_rate": "double", "worker_utilisation": "double", "hours_per_100_contacted": "double",
    "revenue_per_100_contacted": "double", "action_score": "double", "hours": "double", "slot_hours": "double",
    "priority_tier": "int64", "referrals": "int64", "attributed_org_contacted": "int64", "focus_rank": "int64",
}
_STRING_SUFFIX = re.compile(r"(_code|_ref|_name|_label|_status|_display|_list|_note|_basis|_model|_gaps|_text|"
                            r"_reason|_url|_detail|_email|_icon|_title|_type|_level|_method|_source|_by|_key)$")
_INT_PREFIX = re.compile(r"^(is|has|reached|found|counts|includes|needs|cap|requires)_")


def column_type(column: str) -> str:
    """Power BI data type of an exported column (see test_export_types for the data check)."""
    if column in _EXPLICIT_TYPES:
        return _EXPLICIT_TYPES[column]
    if _INT_PREFIX.match(column):
        return "int64"
    if column in _STRING_OVERRIDES or _STRING_SUFFIX.search(column):
        return "string"
    if _DATE_PATTERNS.search(column):
        return "date"
    if _DATETIME_PATTERNS.search(column):
        return "dateTime"
    if _INT_PATTERNS.search(column):
        return "int64"
    if re.search(r"(hours|value|rate|amount|revenue|mrr|probability|utilisation|score|target|gap|"
                  r"pct|received|invoiced|conversion|_weekly|monthly)", column):
        return "double"
    return "string"


def pbi_views(conn: sqlite3.Connection) -> list[str]:
    return [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'view' AND name LIKE 'pbi\\_%' ESCAPE '\\' ORDER BY name")]


def view_columns(conn: sqlite3.Connection, view: str) -> list[str]:
    return [r[1] for r in conn.execute(f"PRAGMA table_info({view})")]


def export_schema(conn: sqlite3.Connection) -> dict[str, dict[str, str]]:
    """{table: {column: type}} for every exported table, in export order."""
    schema = {v: {c: column_type(c) for c in view_columns(conn, v)} for v in pbi_views(conn)}
    schema["pbi_dim_date"] = {c: t for c, t in _DATE_DIM_COLUMNS}
    schema["pbi_dim_dq_check"] = {"check_code": "string", "check_name": "string", "severity": "string"}
    return schema


_DATE_DIM_COLUMNS = [
    ("date", "date"), ("year", "int64"), ("month_number", "int64"), ("month_name", "string"),
    ("year_month", "string"), ("month_start", "date"), ("week_start", "date"), ("iso_week", "int64"),
    ("day_of_week", "int64"), ("day_name", "string"), ("is_weekend", "int64"), ("is_business_day", "int64"),
    ("financial_year", "string"), ("relative_day", "int64"),
]


def _date_rows(cfg: Config, as_of: date, first_data_date: date | None) -> list[list]:
    start = date.fromisoformat(cfg.get("export.calendar_start", "2026-06-01"))
    if first_data_date and first_data_date < start:
        start = first_data_date
    end = as_of + timedelta(days=int(cfg.get("export.calendar_days_ahead", 120)))
    holidays = cfg.public_holidays
    rows, d = [], start
    while d <= end:
        fy = d.year + 1 if d.month >= 7 else d.year
        rows.append([
            d.isoformat(), d.year, d.month, d.strftime("%b"), d.strftime("%Y-%m"),
            d.replace(day=1).isoformat(), (d - timedelta(days=d.weekday())).isoformat(), d.isocalendar()[1],
            d.isoweekday(), d.strftime("%a"), int(d.weekday() >= 5),
            int(d.weekday() < 5 and d.isoformat() not in holidays), f"FY{fy}", (d - as_of).days,
        ])
        d += timedelta(days=1)
    return rows


def _fmt(value) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        return format(value, ".10g") if abs(value) < 1e15 else str(value)
    return str(value)


def _write_csv(path: Path, header: list[str], rows) -> tuple[int, str]:
    tmp = path.with_suffix(".csv.tmp")
    h = hashlib.sha256()
    n = 0
    with open(tmp, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(header)
        for row in rows:
            vals = [_fmt(v) for v in row]
            w.writerow(vals)
            h.update("\x1f".join(vals).encode())
            n += 1
    os.replace(tmp, path)
    return n, h.hexdigest()[:16]


def export_all(conn: sqlite3.Connection, cfg: Config, out_dir: Path | None = None, *, at=None) -> dict:
    out_dir = Path(out_dir or cfg.export_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest: dict = {"generator": f"gcdc {__version__}", "exported_at_utc": now_iso(), "tables": {}}
    with pinned_now(conn, at, cfg.utc_offset_hours) as stamp:
        meta = dict(conn.execute("SELECT * FROM pbi_meta").fetchone())
        manifest.update(as_of_utc=stamp, as_of_local=meta["as_of_local"], schema_version=meta["schema_version"])
        written = set()
        for view in pbi_views(conn):
            cur = conn.execute(f"SELECT * FROM {view}")
            header = [d[0] for d in cur.description]
            n, digest = _write_csv(out_dir / f"{view}.csv", header, cur)
            manifest["tables"][view] = {"rows": n, "sha": digest}
            written.add(f"{view}.csv")
        as_of = date.fromisoformat(meta["as_of_date"])
        first = conn.execute("SELECT MIN(d) FROM (SELECT MIN(date(discovered_at)) AS d FROM opportunities "
                             "UNION ALL SELECT MIN(invoice_date) FROM revenue_transactions "
                             "UNION ALL SELECT MIN(date(sent_at)) FROM outreach)").fetchone()[0]
        n, digest = _write_csv(out_dir / "pbi_dim_date.csv", [c for c, _ in _DATE_DIM_COLUMNS],
                               _date_rows(cfg, as_of, date.fromisoformat(first) if first else None))
        manifest["tables"]["pbi_dim_date"] = {"rows": n, "sha": digest}
        n, digest = _write_csv(out_dir / "pbi_dim_dq_check.csv", ["check_code", "check_name", "severity"],
                               [(c.code, c.name, c.severity) for c in CHECKS])
        manifest["tables"]["pbi_dim_dq_check"] = {"rows": n, "sha": digest}
        written |= {"pbi_dim_date.csv", "pbi_dim_dq_check.csv"}
    for stale in out_dir.glob("pbi_*.csv"):
        if stale.name not in written:
            stale.unlink()
    manifest["schema"] = export_schema(conn)
    tmp = out_dir / "manifest.json.tmp"
    tmp.write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    os.replace(tmp, out_dir / "manifest.json")
    readme = out_dir / "_READ_ME.txt"
    if not readme.exists():
        readme.write_text(
            "GCDC Command Centre — Power BI export\n\n"
            "These files are generated by `gcdc export` / `gcdc refresh` from the central GCDC database.\n"
            "Do not edit them: every refresh overwrites them. Change data through the agents or `gcdc ingest`.\n",
            encoding="utf-8")
    return {"dir": str(out_dir), "as_of_local": manifest["as_of_local"],
            "tables": {k: v["rows"] for k, v in manifest["tables"].items()}}
