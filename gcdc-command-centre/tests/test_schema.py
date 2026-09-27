from conftest import one

from gcdc.db import migrate

REQUIRED_TABLES = [
    "organisations", "contacts", "opportunities", "outreach", "follow_ups", "replies", "meetings",
    "participant_referrals", "trial_shifts", "participants", "rosters", "revenue_transactions", "workers",
    "worker_availability", "partnerships", "associate_provider_applications", "suppression", "agent_runs",
    "alerts", "daily_metrics_snapshot", "organisation_identifiers", "opportunity_stage_history", "business_targets",
    "data_quality_results", "daily_briefs",
]


def test_required_tables_exist(conn):
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    missing = [t for t in REQUIRED_TABLES if t not in tables]
    assert not missing


def test_migrate_is_idempotent(conn):
    assert migrate(conn) == []
    assert one(conn, "SELECT COUNT(*) FROM sqlite_master WHERE type = 'view' AND name LIKE 'pbi_%'") > 30


def test_foreign_keys_enforced(conn):
    assert one(conn, "PRAGMA foreign_keys") == 1


def test_every_pbi_view_runs(conn):
    for (view,) in conn.execute("SELECT name FROM sqlite_master WHERE type = 'view'").fetchall():
        conn.execute(f"SELECT * FROM {view} LIMIT 1").fetchall()


def test_journey_ids_link_end_to_end(demo):
    """Found -> contacted -> reply -> meeting -> referral -> trial -> roster -> revenue share organisation ids."""
    org = one(demo, "SELECT organisation_id FROM rosters WHERE roster_ref = 'DEMO-R-1'")
    for table in ("opportunities", "outreach", "replies", "meetings", "trial_shifts", "pbi_fact_revenue"):
        assert one(demo, f"SELECT COUNT(*) FROM {table} WHERE organisation_id = ?", org) >= 1, table
    assert one(demo, "SELECT COUNT(*) FROM participant_referrals WHERE referring_organisation_id = ?", org) == 1
    # ...and the roster, referral and revenue hang off the same opportunity.
    opp = one(demo, "SELECT opportunity_id FROM rosters WHERE roster_ref = 'DEMO-R-1'")
    assert opp is not None
    assert one(demo, "SELECT COUNT(*) FROM participant_referrals WHERE opportunity_id = ?", opp) == 1
    # Revenue is attributed through its roster (raw rows may only carry the roster).
    assert one(demo, "SELECT COUNT(*) FROM pbi_fact_revenue WHERE opportunity_id = ?", opp) >= 1


def test_stage_history_recorded(demo):
    assert one(demo, "SELECT COUNT(*) FROM opportunity_stage_history") >= one(demo, "SELECT COUNT(*) FROM opportunities")
