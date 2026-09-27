"""`gcdc` command line — the one entry point for agents, schedulers and people.

Run `gcdc --help` or `gcdc <command> --help`. Every command that changes data
logs an agent run, so it shows on the Agent Health page.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, timedelta
from pathlib import Path

from gcdc import __version__
from gcdc.config import load_config
from gcdc.db import open_db, sync_config


def _print(obj, as_json: bool = True) -> None:
    if as_json:
        print(json.dumps(obj, indent=2, default=str))
    else:
        print(obj)


def _load_records(path: str) -> tuple[list[dict], dict]:
    raw = sys.stdin.read() if path == "-" else Path(path).read_text(encoding="utf-8")
    raw = raw.strip()
    if raw.startswith("["):
        return json.loads(raw), {}
    if raw.startswith("{"):
        doc = json.loads(raw)
        if "records" in doc:
            return doc["records"], {k: v for k, v in doc.items() if k != "records"}
        return [doc], {}
    return [json.loads(line) for line in raw.splitlines() if line.strip()], {}  # JSON lines


# ---------------------------------------------------------------------------
def cmd_init(args, cfg):
    conn = open_db(cfg)
    result = sync_config(conn, cfg)
    _print({"database": str(cfg.db_path), "export_dir": str(cfg.export_dir), "config": str(cfg.path), **result})


def cmd_config(args, cfg):
    conn = open_db(cfg)
    if args.action == "sync":
        _print(sync_config(conn, cfg))
    else:
        rows = conn.execute("SELECT target_code, target_value, unit, effective_from FROM business_targets "
                            "ORDER BY target_code, effective_from").fetchall()
        _print({"config": str(cfg.path), "targets": [dict(r) for r in rows],
                "settings": {r[0]: r[1] for r in conn.execute("SELECT key, value FROM settings ORDER BY key")}})


def cmd_ingest(args, cfg):
    from gcdc.agents import finish_run, start_run
    from gcdc.ingest import IngestContext, ingest_records

    conn = open_db(cfg)
    records, meta = _load_records(args.file)
    agent = (args.agent or meta.get("agent") or "MANUAL").upper()
    ctx = IngestContext(conn, cfg, agent_code=agent, source_system=args.source_system or meta.get("source_system"),
                        allow_suppressed=args.allow_suppressed)
    run_id = None if args.dry_run or args.no_run_log else start_run(conn, agent, trigger=args.trigger)
    ctx.run_id = run_id
    conn.execute("BEGIN IMMEDIATE")
    result = ingest_records(ctx, records)
    conn.execute("ROLLBACK" if args.dry_run else "COMMIT")
    if run_id:
        status = "FAILED" if result.failed and not result.succeeded else "WARNING" if result.failed else "SUCCESS"
        finish_run(conn, run_id, status=status, processed=result.processed, created=result.created,
                   updated=result.updated, skipped=result.failed, errors=result.failed,
                   warnings=len(result.warnings),
                   last_error=result.errors[0]["error"] if result.errors else None,
                   next_scheduled_run=meta.get("next_scheduled_run"))
    out = result.as_dict()
    out.update(dry_run=args.dry_run, run_id=run_id)
    _print(out)
    return 1 if result.failed and not result.succeeded else 0


def cmd_run(args, cfg):
    from gcdc.agents import finish_run, start_run

    conn = open_db(cfg)
    if args.action == "start":
        _print({"run_id": start_run(conn, args.agent, trigger=args.trigger)})
    else:
        finish_run(conn, args.run_id, status=args.status, processed=args.processed, created=args.created,
                   updated=args.updated, errors=args.errors, warnings=args.warnings, last_error=args.error,
                   next_scheduled_run=args.next_run)
        _print({"run_id": args.run_id, "status": args.status.upper()})


def cmd_import(args, cfg):
    from gcdc.importers.runner import import_all, import_canonical, import_file, import_mailbox_json

    conn = open_db(cfg)
    if args.kind == "all":
        _print(import_all(conn, cfg, Path(args.file), dry_run=args.dry_run, follow_ups=not args.no_follow_ups))
    elif args.kind == "canonical":
        if not args.entity:
            raise SystemExit("--entity is required for canonical imports")
        _print(import_canonical(conn, cfg, Path(args.file), args.entity, dry_run=args.dry_run))
    elif args.kind == "mailbox-json":
        _print(import_mailbox_json(conn, cfg, Path(args.file), dry_run=args.dry_run))
    else:
        _print(import_file(conn, cfg, args.kind, Path(args.file), dry_run=args.dry_run,
                           follow_ups=not args.no_follow_ups))


def cmd_sync(args, cfg):
    from gcdc.importers.graph import sync_outlook

    conn = open_db(cfg)
    _print(sync_outlook(conn, cfg, days=args.days))


def cmd_check_send(args, cfg):
    from gcdc.presend import check_send

    conn = open_db(cfg)
    verdict = check_send(conn, cfg, to_email=args.to, organisation=args.org, subject=args.subject)
    _print(verdict)
    return 0 if verdict["ok"] else 2


def cmd_reconcile(args, cfg):
    from gcdc.reconcile import reconcile

    _print(reconcile(open_db(cfg)))


def cmd_dq(args, cfg):
    from gcdc.quality import CHECK_NAMES, run_checks

    result = run_checks(open_db(cfg))
    result["by_check"] = {f"{k} — {CHECK_NAMES[k]}": v for k, v in result["by_check"].items()}
    _print(result)


def cmd_snapshot(args, cfg):
    from gcdc.snapshot import snapshot_catch_up, take_snapshot
    from gcdc.timeutil import local_today

    conn = open_db(cfg)
    today = local_today(cfg.utc_offset_hours)
    if args.date in (None, "yesterday"):
        _print({"written": snapshot_catch_up(conn, offset_hours=cfg.utc_offset_hours)})
    else:
        d = today if args.date == "today" else date.fromisoformat(args.date)
        _print({"date": d.isoformat(), "written": take_snapshot(conn, d, offset_hours=cfg.utc_offset_hours,
                                                                 catch_up=d < today - timedelta(days=1))})


def cmd_export(args, cfg):
    from gcdc.export import export_all

    _print(export_all(open_db(cfg), cfg, Path(args.dir) if args.dir else None))


def cmd_refresh(args, cfg):
    from gcdc.pipeline import refresh

    summary = refresh(open_db(cfg), cfg, export_dir=Path(args.dir) if args.dir else None,
                      snapshots=not args.no_snapshot, trigger=args.trigger)
    if not args.verbose:
        summary["export"] = {"dir": summary["export"]["dir"], "as_of_local": summary["export"]["as_of_local"],
                             "tables": len(summary["export"]["tables"])}
    _print(summary)


def cmd_backup(args, cfg):
    from gcdc.db import backup

    dest = Path(args.dir) if args.dir else cfg._path(cfg.get("database.backup_dir", "data/backups"))
    keep = args.keep if args.keep is not None else int(cfg.get("database.backup_keep", 30))
    path = backup(open_db(cfg), dest, keep=keep)
    _print({"backup": str(path), "bytes": path.stat().st_size, "kept": len(list(dest.glob("gcdc-*.db")))})


def cmd_status(args, cfg):
    conn = open_db(cfg)
    k = dict(conn.execute("SELECT * FROM pbi_kpi_current").fetchone())
    if args.json:
        _print(k)
        return
    money = lambda v: "UNKNOWN" if v is None else f"${v:,.0f}"
    print(f"GCDC status — {k['as_of_local']}")
    print(f"  MRR {money(k['mrr'])} of {money(k['revenue_target'])} target (gap {money(k['revenue_gap'])})"
          f"   weekly hours {k['weekly_billable_hours']:g} (target {k['weekly_hours_target_min']:g}-{k['weekly_hours_target_max']:g})")
    print(f"  Active participants {k['active_participants']} | rosters {k['active_rosters']}"
          f" | active opportunities {k['active_opportunities']} (P1 {k['p1_open']})")
    print(f"  Pipeline known {money(k['pipeline_monthly_value_known'])}/mo + {k['pipeline_value_unknown_count']} UNKNOWN"
          f" | near-term {money(k['near_term_monthly_value_known'])}/mo")
    print(f"  Today: {k['emails_scheduled_today']} emails scheduled, {k['followups_due_today']} follow-ups due,"
          f" {k['followups_overdue']} overdue, {k['replies_awaiting_response']} replies waiting,"
          f" {k['referrals_needing_action']} referrals to action")
    print(f"  Agents RED {k['agents_red']} AMBER {k['agents_amber']} GREEN {k['agents_green']}"
          f" | data-quality errors {k['dq_errors']} warnings {k['dq_warnings']}")


def cmd_brief(args, cfg):
    from gcdc.brief import run_brief

    conn = open_db(cfg)
    result = run_brief(conn, cfg, brief_date=date.fromisoformat(args.date) if args.date else None,
                       use_ai=args.ai, save=not args.no_save, force=args.force)
    print(result["markdown"])
    if result.get("path"):
        print(f"\n[saved to {result['path']}]", file=sys.stderr)
        if not args.no_export:  # so the next Power BI refresh shows today's focus
            from gcdc.export import export_all

            export_all(conn, cfg)


def cmd_legacy(args, cfg):
    from gcdc.importers.legacy import audit_legacy, import_legacy

    if args.action == "audit":
        _print(audit_legacy(Path(args.path)))
    else:
        if not args.mapping:
            raise SystemExit("--mapping is required for legacy import (see mappings/legacy_outreach_agent.example.toml)")
        _print(import_legacy(open_db(cfg), cfg, Path(args.path), Path(args.mapping), dry_run=args.dry_run))


def cmd_merge(args, cfg):
    from gcdc.merge import merge_organisations

    _print(merge_organisations(open_db(cfg), keep_id=args.keep, merge_id=args.merge))


def cmd_demo(args, cfg):
    from gcdc.demo import seed_demo

    conn = open_db(cfg)
    _print(seed_demo(conn, cfg))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="gcdc", description="GCDC Command Centre — central database & Power BI feed")
    p.add_argument("--config", help="path to gcdc.toml (default: config/gcdc.toml or $GCDC_CONFIG)")
    p.add_argument("--version", action="version", version=f"gcdc {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="create/upgrade the database and sync config").set_defaults(func=cmd_init)

    s = sub.add_parser("config", help="sync or show business configuration (targets, thresholds)")
    s.add_argument("action", choices=["sync", "show"])
    s.set_defaults(func=cmd_config)

    s = sub.add_parser("ingest", help="write agent records (JSON / JSON-lines) to the central database")
    s.add_argument("file", help="path to JSON, or - for stdin")
    s.add_argument("--agent", help="agent code, e.g. OUTREACH (logged as an agent run)")
    s.add_argument("--source-system", help="namespace for source_key idempotency (default: agent code)")
    s.add_argument("--trigger", default="SCHEDULED", choices=["SCHEDULED", "MANUAL", "CATCH_UP"])
    s.add_argument("--allow-suppressed", action="store_true", help="record outreach even to suppressed addresses")
    s.add_argument("--dry-run", action="store_true", help="validate and report, write nothing")
    s.add_argument("--no-run-log", action="store_true", help="do not log an agent run (use inside gcdc run start/finish)")
    s.set_defaults(func=cmd_ingest)

    s = sub.add_parser("run", help="log agent runs for agents that are not Python")
    rs = s.add_subparsers(dest="action", required=True)
    a = rs.add_parser("start")
    a.add_argument("--agent", required=True)
    a.add_argument("--trigger", default="SCHEDULED", choices=["SCHEDULED", "MANUAL", "CATCH_UP"])
    a = rs.add_parser("finish")
    a.add_argument("--run-id", type=int, required=True)
    a.add_argument("--status", required=True, choices=["SUCCESS", "WARNING", "FAILED", "ABORTED",
                                                       "success", "warning", "failed", "aborted"])
    for name in ("processed", "created", "updated", "errors", "warnings"):
        a.add_argument(f"--{name}", type=int, default=0)
    a.add_argument("--error", help="last error message")
    a.add_argument("--next-run", help="next scheduled run (ISO timestamp)")
    s.set_defaults(func=cmd_run)

    s = sub.add_parser("import", help="import existing GCDC sheets, canonical CSV templates or mailbox JSON")
    s.add_argument("kind", help="campaign-lists | sah-provider-db | crm-master | target-list | referral-crm | "
                                "coordinator-emails | outreach-queue | facebook-radar | all | canonical | mailbox-json")
    s.add_argument("file", help="file to import (for 'all': a folder of downloaded sheets)")
    s.add_argument("--entity", help="for canonical imports: worker, worker_availability, participant, roster, "
                                    "revenue, referral, meeting, trial_shift, partnership, application, suppression, "
                                    "compliance_document, organisation, contact, opportunity, worker_requirement")
    s.add_argument("--dry-run", action="store_true")
    s.add_argument("--no-follow-ups", action="store_true", help="do not create follow-up tasks from sequence rules")
    s.set_defaults(func=cmd_import)

    s = sub.add_parser("sync", help="deterministic mailbox sync via Microsoft Graph (sent, replies, bounces)")
    s.add_argument("source", choices=["outlook"])
    s.add_argument("--days", type=int, default=3, help="look-back window")
    s.set_defaults(func=cmd_sync)

    s = sub.add_parser("check-send", help="pre-send check for one email (suppression, bounces, duplicates)")
    s.add_argument("--to", required=True)
    s.add_argument("--org")
    s.add_argument("--subject")
    s.set_defaults(func=cmd_check_send)

    sub.add_parser("reconcile", help="link stray events, advance stages, close done follow-ups").set_defaults(func=cmd_reconcile)
    sub.add_parser("dq", help="run data-quality checks").set_defaults(func=cmd_dq)

    s = sub.add_parser("snapshot", help="write daily metrics snapshot(s); default: catch up through yesterday")
    s.add_argument("--date", help="YYYY-MM-DD | today | yesterday")
    s.set_defaults(func=cmd_snapshot)

    s = sub.add_parser("export", help="export Power BI CSVs")
    s.add_argument("--dir")
    s.set_defaults(func=cmd_export)

    s = sub.add_parser("refresh", help="full pipeline: config sync, reconcile, DQ, snapshots, export")
    s.add_argument("--dir")
    s.add_argument("--no-snapshot", action="store_true")
    s.add_argument("--trigger", default="SCHEDULED", choices=["SCHEDULED", "MANUAL", "CATCH_UP"])
    s.add_argument("--verbose", action="store_true")
    s.set_defaults(func=cmd_refresh)

    s = sub.add_parser("backup", help="consistent copy of the database (keeps the newest N)")
    s.add_argument("--dir", help="backup folder (default: [database] backup_dir)")
    s.add_argument("--keep", type=int, help="how many daily copies to keep (default: [database] backup_keep)")
    s.set_defaults(func=cmd_backup)

    s = sub.add_parser("status", help="print the headline numbers")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_status)

    s = sub.add_parser("brief", help="Daily BI Agent: produce the morning management brief")
    s.add_argument("--date", help="brief date (default today)")
    s.add_argument("--ai", action="store_true", help="add a Claude reasoning pass to choose today's focus")
    s.add_argument("--no-save", action="store_true")
    s.add_argument("--force", action="store_true", help="run even on weekends / public holidays")
    s.add_argument("--no-export", action="store_true", help="do not refresh the Power BI export after saving")
    s.set_defaults(func=cmd_brief)

    s = sub.add_parser("legacy", help="audit or import an existing agent SQLite database")
    s.add_argument("action", choices=["audit", "import"])
    s.add_argument("path")
    s.add_argument("--mapping")
    s.add_argument("--dry-run", action="store_true")
    s.set_defaults(func=cmd_legacy)

    s = sub.add_parser("merge-orgs", help="merge a duplicate organisation into another")
    s.add_argument("--keep", type=int, required=True)
    s.add_argument("--merge", type=int, required=True)
    s.set_defaults(func=cmd_merge)

    s = sub.add_parser("demo", help="load clearly-fictional demo data (for trying the dashboard)")
    s.add_argument("action", choices=["seed"])
    s.set_defaults(func=cmd_demo)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cfg = load_config(args.config)
    return args.func(args, cfg) or 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
