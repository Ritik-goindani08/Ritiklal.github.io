"""Run importers through the ingest layer, logged as agent runs."""

from __future__ import annotations

import csv
import json
import sqlite3
from datetime import date
from pathlib import Path

from gcdc.agents import finish_run, start_run
from gcdc.config import Config
from gcdc.ingest import RECORD_TYPES, IngestContext, ingest_records
from gcdc.importers.sheets import IMPORT_ORDER, IMPORTERS, ImportOptions
from gcdc.timeutil import local_today

# File-name patterns used by `gcdc import all <folder>` (case-insensitive, all words must appear).
FILE_PATTERNS = {
    "campaign-lists": ["campaign", "list"],
    "sah-provider-db": ["support", "home", "provider"],
    "crm-master": ["crm", "master"],
    "target-list": ["target", "list"],
    "referral-crm": ["referral", "crm"],
    "coordinator-emails": ["coordinator", "email"],
    "outreach-queue": ["outreach", "queue"],
    "facebook-radar": ["radar"],
}


def _options(cfg: Config, follow_ups: bool) -> ImportOptions:
    return ImportOptions(
        today=local_today(cfg.utc_offset_hours),
        records_start=date.fromisoformat(cfg.get("business.records_start_date", "2026-01-01")),
        follow_up_days=tuple(cfg.get("outreach.follow_up_days", [7, 17])),
        create_follow_ups=follow_ups)


def _ingest(conn: sqlite3.Connection, cfg: Config, records: list[dict], *, agent: str, system: str,
            dry_run: bool) -> tuple[dict, int | None]:
    ctx = IngestContext(conn, cfg, agent_code=agent, source_system=system)
    run_id = None if dry_run else start_run(conn, agent, trigger="MANUAL")
    ctx.run_id = run_id
    conn.execute("BEGIN IMMEDIATE")
    try:
        result = ingest_records(ctx, records)
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("ROLLBACK" if dry_run else "COMMIT")
    if run_id:
        finish_run(conn, run_id, status="WARNING" if result.failed else "SUCCESS", processed=result.processed,
                   created=result.created, updated=result.updated, skipped=result.failed, errors=result.failed,
                   warnings=len(result.warnings), last_error=result.errors[0]["error"] if result.errors else None,
                   details={"system": system})
    return result.as_dict(), run_id


def import_file(conn: sqlite3.Connection, cfg: Config, kind: str, path: Path, *, dry_run: bool = False,
                follow_ups: bool = True) -> dict:
    if kind not in IMPORTERS:
        raise SystemExit(f"unknown import kind {kind!r}; choose from {', '.join(IMPORTERS)}")
    plan = IMPORTERS[kind](path, _options(cfg, follow_ups))
    result, run_id = _ingest(conn, cfg, plan.records, agent="SHEET_IMPORT", system=f"sheet:{kind}", dry_run=dry_run)
    return {"kind": kind, "file": str(path), "dry_run": dry_run, "run_id": run_id, "rows_read": plan.rows_read,
            "rows_skipped": plan.rows_skipped, "records": result["processed"], "failed": result["failed"],
            "stats": result["stats"], "problems": plan.problems[:100], "problem_count": len(plan.problems),
            "errors": result["errors"][:25], "warnings": result["warnings"][:25]}


def find_sheet_files(folder: Path) -> dict[str, Path]:
    found: dict[str, Path] = {}
    files = [p for p in folder.iterdir() if p.suffix.lower() in (".xlsx", ".csv")]
    for kind, words in FILE_PATTERNS.items():
        matches = [p for p in files if all(w in p.stem.lower().replace("_", " ").replace("-", " ") for w in words)]
        if matches:
            found[kind] = max(matches, key=lambda p: p.stat().st_mtime)
    return found


def import_all(conn: sqlite3.Connection, cfg: Config, folder: Path, *, dry_run: bool = False,
               follow_ups: bool = True) -> dict:
    files = find_sheet_files(folder)
    results = []
    for kind in IMPORT_ORDER:
        if kind in files:
            r = import_file(conn, cfg, kind, files[kind], dry_run=dry_run, follow_ups=follow_ups)
            r.pop("errors", None)
            r["problems"] = r["problems"][:10]
            results.append(r)
    return {"folder": str(folder), "imported": [r["kind"] for r in results],
            "not_found": [k for k in IMPORT_ORDER if k not in files], "results": results}


CANONICAL_ENTITIES = {k for k in RECORD_TYPES if k not in ("bounce", "alert")}


def import_canonical(conn: sqlite3.Connection, cfg: Config, path: Path, entity: str, *, dry_run: bool = False) -> dict:
    """CSV whose headers are canonical field names (see templates/). Blank cells are ignored."""
    entity = entity.strip().lower()
    if entity not in CANONICAL_ENTITIES:
        raise SystemExit(f"unknown entity {entity!r}; choose from {', '.join(sorted(CANONICAL_ENTITIES))}")
    with open(path, newline="", encoding="utf-8-sig") as fh:
        rows = list(csv.DictReader(fh))
    records = []
    for row in rows:
        rec = {"type": entity}
        org = {}
        for key, value in row.items():
            if key is None or value is None or not str(value).strip():
                continue
            key = key.strip()
            if key.startswith("organisation."):
                org[key.split(".", 1)[1]] = value.strip()
            else:
                rec[key] = value.strip()
        if org:
            rec["organisation"] = org
        # Template example rows are marked EXAMPLE (in any field, organisation fields included).
        if any(str(v).upper().startswith("EXAMPLE") for v in [*rec.values(), *org.values()] if isinstance(v, str)):
            continue
        records.append(rec)
    result, run_id = _ingest(conn, cfg, records, agent="MANUAL", system=f"csv:{entity}", dry_run=dry_run)
    return {"entity": entity, "file": str(path), "dry_run": dry_run, "run_id": run_id, "rows": len(rows),
            **{k: result[k] for k in ("processed", "failed", "created", "updated", "errors", "warnings")}}


def import_mailbox_json(conn: sqlite3.Connection, cfg: Config, path: Path, *, dry_run: bool = False) -> dict:
    from gcdc.importers.mailbox import mailbox_records

    doc = json.loads(path.read_text(encoding="utf-8"))
    messages = doc.get("messages", doc) if isinstance(doc, dict) else doc
    owner = (doc.get("mailbox") if isinstance(doc, dict) else None) or cfg.get("graph.mailbox")
    if not owner:
        raise SystemExit("mailbox owner unknown: add 'mailbox' to the JSON or [graph] mailbox to gcdc.toml")
    records, skipped = mailbox_records(conn, messages, owner=owner, free_mail=cfg.free_mail_domains)
    result, run_id = _ingest(conn, cfg, records, agent="MAILBOX_SYNC", system="mailbox", dry_run=dry_run)
    return {"messages": len(messages), "records": len(records), "ignored_unrelated": skipped, "run_id": run_id,
            **{k: result[k] for k in ("processed", "failed", "created", "updated", "stats", "errors", "warnings")}}
