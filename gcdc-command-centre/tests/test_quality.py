"""Data-quality checks flag the problems the dashboard must warn about."""

from conftest import one

from gcdc.quality import CHECKS, run_checks


def issues(conn):
    return run_checks(conn)["by_check"]


def test_clean_minimal_data_has_no_errors(conn, ingest):
    ingest([{"type": "organisation", "name": "Clean Org", "region": "Gold Coast"}])
    assert run_checks(conn)["errors"] == 0


def test_flags_known_problems(conn, ingest):
    org = {"name": "Messy Org", "region": "Gold Coast"}
    r = ingest([
        {"type": "organisation", **org},
        {"type": "opportunity", "organisation": org, "opportunity_type_code": "SUBCONTRACTING"},  # no source
        {"type": "participant", "participant_ref": "P-9", "status": "ACTIVE", "organisation": org},  # no roster
        {"type": "roster", "roster_ref": "R-9", "organisation": org, "weekly_hours": 5, "status": "ACTIVE",
         "start_date": "2026-09-01"},  # rate unknown
        {"type": "worker", "worker_ref": "W-9", "display_name": "Unverified", "status": "ACTIVE"},
        {"type": "referral", "organisation": org, "participant_ref": "P-10", "funding_type": "NDIS_AGENCY_MANAGED",
         "received_at": "2026-09-01T00:00:00Z", "source_key": "ref-9"},
    ])
    assert r.failed == 0, r.errors
    # Impossible dates can only arrive from outside the ingest layer (e.g. a hand-edited DB).
    conn.execute("UPDATE rosters SET end_date = '2026-01-01' WHERE roster_ref = 'R-9'")
    found = issues(conn)
    for code in ("OPP_NO_SOURCE", "PARTICIPANT_NO_ROSTER", "ROSTER_RATE_UNKNOWN", "WORKER_NOT_VERIFIED",
                 "IMPOSSIBLE_DATES", "AGENCY_MANAGED_REFERRAL"):
        assert found.get(code), code


def test_errors_raise_alerts_that_auto_resolve(conn, ingest):
    org = {"name": "Alert Org"}
    ingest([{"type": "participant", "participant_ref": "P-1", "status": "ACTIVE", "organisation": org}])
    run_checks(conn)
    assert one(conn, "SELECT COUNT(*) FROM alerts WHERE alert_key = 'dq-PARTICIPANT_NO_ROSTER' AND status <> 'RESOLVED'") == 1
    conn.execute("UPDATE participants SET status = 'ENDED'")
    run_checks(conn)
    assert one(conn, "SELECT status FROM alerts WHERE alert_key = 'dq-PARTICIPANT_NO_ROSTER'") == "RESOLVED"


def test_results_are_exposed_to_power_bi(conn, ingest):
    ingest([{"type": "opportunity", "organisation": {"name": "No Source Org"}, "opportunity_type_code": "OTHER"}])
    run_checks(conn)
    assert one(conn, "SELECT COUNT(*) FROM pbi_fact_data_quality WHERE check_code = 'OPP_NO_SOURCE'") == 1
    assert len({c.code for c in CHECKS}) == len(CHECKS)
