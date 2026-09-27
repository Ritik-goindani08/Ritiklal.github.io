"""End-to-end refresh, agent health RAG, Action Centre, pre-send checks and organisation merges."""

from datetime import timedelta

from conftest import one

from gcdc.agents import agent_run
from gcdc.merge import merge_organisations
from gcdc.pipeline import refresh
from gcdc.presend import check_send
from gcdc.timeutil import now_utc, to_iso_utc


def test_refresh_end_to_end(demo, cfg, tmp_path):
    summary = refresh(demo, cfg, export_dir=tmp_path / "out")
    assert summary["snapshots_written"]  # yesterday's snapshot
    assert (tmp_path / "out" / "pbi_kpi_current.csv").exists()
    run = demo.execute("SELECT status FROM agent_runs WHERE agent_code = 'REFRESH_PIPELINE' ORDER BY run_id DESC").fetchone()
    assert run[0] in ("SUCCESS", "WARNING")
    # A second refresh the same day writes no new snapshot.
    assert refresh(demo, cfg, export_dir=tmp_path / "out")["snapshots_written"] == []


def test_agent_health_rag(demo):
    rag = dict(demo.execute("SELECT agent_code, rag_status FROM pbi_fact_agent_health").fetchall())
    assert rag["EMAIL_VERIFICATION"] == "RED"      # last run failed
    assert rag["INBOX"] == "AMBER"                 # last run finished with warnings
    assert rag["OUTREACH"] == "GREEN"
    assert rag["WEEKLY_PLANNER"] == "RED"          # never reported


def test_agent_run_context_manager_records_failures(conn):
    try:
        with agent_run(conn, "OUTREACH") as run:
            run.processed = 3
            raise RuntimeError("SMTP down")
    except RuntimeError:
        pass
    row = conn.execute("SELECT status, last_error FROM agent_runs ORDER BY run_id DESC").fetchone()
    assert row["status"] == "FAILED" and "SMTP down" in row["last_error"]
    assert one(conn, "SELECT rag_status FROM pbi_fact_agent_health WHERE agent_code = 'OUTREACH'") == "RED"


def test_action_centre_ranks_money_and_urgency(demo):
    rows = demo.execute("SELECT category, priority, due_status, action_score FROM pbi_action_centre "
                        "ORDER BY action_score DESC").fetchall()
    assert rows, "action centre is empty"
    scores = [r["action_score"] for r in rows]
    assert scores == sorted(scores, reverse=True)
    top = rows[0]
    assert top["priority"] == "P1" and top["due_status"] in ("OVERDUE", "DUE_TODAY")
    categories = {r["category"] for r in rows}
    for expected in ("Follow-up", "Referral", "Needs approval"):
        assert expected in categories
    # Silent agents are summarised in one row, not one per agent.
    assert one(demo, "SELECT COUNT(*) FROM pbi_action_centre WHERE category LIKE 'Agent%' AND next_action LIKE "
                     "'%not reporting%'") <= 1


def test_presend_blocks_suppressed_bounced_and_duplicate(conn, cfg, ingest):
    org = {"name": "Presend Org", "website": "presend.example"}
    an_hour_ago = to_iso_utc(now_utc() - timedelta(hours=1))
    ingest([{"type": "suppression", "email": "no@presend.example", "reason": "UNSUBSCRIBE"},
            {"type": "outreach", "organisation": org, "to_email": "sent@presend.example", "status": "SENT",
             "sent_at": an_hour_ago, "external_message_id": "<p1@x>"},
            {"type": "outreach", "organisation": org, "to_email": "gone@presend.example", "status": "SENT",
             "sent_at": "2026-09-01T00:00:00Z", "external_message_id": "<p2@x>"},
            {"type": "bounce", "to_email": "gone@presend.example", "bounced_at": "2026-09-01T00:05:00Z"}])
    assert check_send(conn, cfg, to_email="no@presend.example")["ok"] is False
    assert check_send(conn, cfg, to_email="gone@presend.example")["ok"] is False
    assert check_send(conn, cfg, to_email="sent@presend.example")["ok"] is False  # sent within the last 3 days
    fresh = check_send(conn, cfg, to_email="new@presend.example")
    assert fresh["ok"] is True and fresh["warnings"]  # warns the org was already contacted at another address


def test_merge_moves_history_and_future_matches_go_to_survivor(conn, ingest):
    ingest([{"type": "organisation", "name": "Lagoon Care", "website": "lagoon.example"},
            {"type": "organisation", "name": "Lagoon Community Services", "website": "lagooncs.example"},
            {"type": "outreach", "organisation": {"website": "lagooncs.example", "name": "Lagoon Community Services"},
             "to_email": "a@lagooncs.example", "status": "SENT", "sent_at": "2026-09-01T00:00:00Z",
             "external_message_id": "<mg@x>"}])
    keep = one(conn, "SELECT organisation_id FROM organisations WHERE name = 'Lagoon Care'")
    gone = one(conn, "SELECT organisation_id FROM organisations WHERE name = 'Lagoon Community Services'")
    merge_organisations(conn, keep_id=keep, merge_id=gone)
    assert one(conn, "SELECT organisation_id FROM outreach") == keep
    ingest([{"type": "contact", "organisation": {"website": "lagooncs.example"}, "email": "b@lagooncs.example"}])
    assert one(conn, "SELECT organisation_id FROM contacts WHERE email = 'b@lagooncs.example'") == keep


def test_backup_is_consistent_and_rotates(demo, tmp_path):
    import sqlite3

    from gcdc.db import backup

    for day in ("2026-09-01", "2026-09-02", "2026-09-03"):
        path = backup(demo, tmp_path, keep=2, stamp=day)
    assert sorted(p.name for p in tmp_path.glob("gcdc-*.db")) == ["gcdc-2026-09-02.db", "gcdc-2026-09-03.db"]
    copy = sqlite3.connect(path)
    assert copy.execute("SELECT COUNT(*) FROM organisations").fetchone()[0] == one(demo, "SELECT COUNT(*) FROM organisations")
    assert copy.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
