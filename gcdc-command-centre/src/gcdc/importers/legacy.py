"""Adopt an existing agent database (e.g. the local GCDC Outreach Agent's SQLite file).

1. `gcdc legacy audit path/to/outreach.db` — read-only inventory of tables,
   columns and row counts, with a suggested mapping file.
2. Edit the mapping (see mappings/legacy_outreach_agent.example.toml).
3. `gcdc legacy import path/to/outreach.db --mapping my_mapping.toml [--dry-run]`

Rows go through the normal ingest layer, so organisations already in the
central database are matched, not duplicated, and re-running is idempotent
(each row's legacy primary key becomes its source_key).
"""

from __future__ import annotations

import sqlite3
import tomllib
from pathlib import Path

from gcdc.config import Config

# Column-name hints used to suggest a record type for each legacy table.
_HINTS = {
    "outreach": {"sent", "sent_at", "subject", "to_email", "message_id", "scheduled", "scheduled_for"},
    "reply": {"reply", "replied", "received_at", "classification", "in_reply_to"},
    "organisation": {"company", "organisation", "organization", "abn", "website", "domain"},
    "contact": {"first_name", "last_name", "full_name", "email", "role", "title"},
    "suppression": {"unsubscribe", "suppressed", "bounce", "do_not_contact", "reason"},
    "opportunity": {"opportunity", "lead", "priority", "score", "stage", "source"},
    "follow_up": {"follow_up", "followup", "due", "due_date", "next_follow_up"},
}
_FIELD_HINTS = {
    "company": "organisation.name", "company_name": "organisation.name", "organisation": "organisation.name",
    "organization": "organisation.name", "org_name": "organisation.name", "website": "organisation.website",
    "abn": "organisation.abn", "email": "to_email", "to_email": "to_email", "recipient": "to_email",
    "subject": "subject", "sent_at": "sent_at", "sent": "sent_at", "scheduled_for": "scheduled_for",
    "scheduled_at": "scheduled_for", "status": "status", "message_id": "external_message_id",
    "internet_message_id": "external_message_id", "thread_id": "thread_id", "conversation_id": "thread_id",
    "region": "organisation.region", "suburb": "organisation.suburb", "phone": "organisation.phone",
}


def _ro(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def audit_legacy(path: Path) -> dict:
    if not path.exists():
        raise SystemExit(f"{path} not found")
    conn = _ro(path)
    tables = []
    for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' "
                                "ORDER BY name"):
        cols = [r[1] for r in conn.execute(f'PRAGMA table_info("{name}")')]
        count = conn.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
        lowered = {c.lower() for c in cols}
        scores = {rtype: len(lowered & hints) for rtype, hints in _HINTS.items()}
        best = max(scores, key=scores.get)
        tables.append({"table": name, "rows": count, "columns": cols,
                       "suggested_record_type": best if scores[best] else None,
                       "suggested_columns": {c: _FIELD_HINTS[c.lower()] for c in cols if c.lower() in _FIELD_HINTS}})
    skeleton = []
    for t in tables:
        if not t["suggested_record_type"]:
            continue
        skeleton.append(f'[[tables]]\nsource = "{t["table"]}"\nrecord_type = "{t["suggested_record_type"]}"\n'
                        f'source_key_column = "{t["columns"][0]}"\n[tables.columns]')
        skeleton += [f'{c} = "{f}"' for c, f in t["suggested_columns"].items()]
        skeleton.append("")
    return {"database": str(path), "tables": tables, "suggested_mapping_toml": "\n".join(skeleton)}


def _nest(flat: dict) -> dict:
    out: dict = {}
    for key, value in flat.items():
        if value is None or (isinstance(value, str) and not value.strip()):
            continue
        parts = key.split(".")
        node = out
        for p in parts[:-1]:
            node = node.setdefault(p, {})
        node[parts[-1]] = value
    return out


def import_legacy(conn: sqlite3.Connection, cfg: Config, path: Path, mapping_path: Path, *, dry_run: bool = False) -> dict:
    from gcdc.importers.runner import _ingest

    mapping = tomllib.loads(mapping_path.read_text(encoding="utf-8"))
    legacy = _ro(path)
    summary = []
    for spec in mapping.get("tables", []):
        rtype = spec["record_type"]
        query = spec.get("query") or f'SELECT * FROM "{spec["source"]}"'
        columns: dict = spec.get("columns", {})
        constants: dict = spec.get("constants", {})
        key_col = spec.get("source_key_column")
        records = []
        for row in legacy.execute(query):
            row = dict(row)
            flat = {target: row.get(src) for src, target in columns.items()}
            flat.update(constants)
            rec = {"type": rtype, **_nest(flat)}
            if key_col and row.get(key_col) is not None and rtype not in ("organisation", "contact", "suppression"):
                rec["source_key"] = str(row[key_col])
            if key_col and rtype == "organisation" and row.get(key_col) is not None:
                rec["external_id"] = f"legacy:{spec.get('source', 'query')}:{row[key_col]}"
            records.append(rec)
        result, run_id = _ingest(conn, cfg, records, agent="LEGACY_IMPORT",
                                 system=f"legacy:{spec.get('source', 'query')}", dry_run=dry_run)
        summary.append({"source": spec.get("source") or query, "record_type": rtype, "rows": len(records),
                        "failed": result["failed"], "created": result["created"], "updated": result["updated"],
                        "errors": result["errors"][:10], "run_id": run_id})
    return {"database": str(path), "dry_run": dry_run, "tables": summary}
