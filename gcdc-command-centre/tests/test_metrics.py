"""Revenue rules: approved-rate MRR, unknown stays unknown, actuals never mixed with pipeline, config targets."""

import pytest

from gcdc.db import sync_config

CORAL = {"name": "Coral Care", "website": "coral.example"}

BOOK = [
    {"type": "participant", "participant_ref": "P-1", "status": "ACTIVE", "organisation": CORAL,
     "start_date": "2026-09-01"},
    {"type": "participant", "participant_ref": "P-2", "status": "ACTIVE", "organisation": CORAL,
     "start_date": "2026-09-01"},
    # Approved rate: counts to MRR.
    {"type": "roster", "roster_ref": "R-1", "participant_ref": "P-1", "organisation": CORAL, "weekly_hours": 10,
     "hourly_rate": 60, "rate_basis": "APPROVED", "status": "ACTIVE", "start_date": "2026-09-01"},
    # Only a quoted rate: hours count, dollars do not.
    {"type": "roster", "roster_ref": "R-2", "participant_ref": "P-2", "organisation": CORAL, "weekly_hours": 5,
     "hourly_rate": 70, "rate_basis": "QUOTED", "status": "ACTIVE", "start_date": "2026-09-01"},
    # Confirmed future start: projected MRR only.
    {"type": "roster", "roster_ref": "R-3", "organisation": CORAL, "weekly_hours": 4, "hourly_rate": 65,
     "rate_basis": "APPROVED", "status": "SCHEDULED", "start_date": "2099-01-01"},
    # Pipeline: one with unknown rate, one fully known.
    {"type": "opportunity", "organisation": CORAL, "opportunity_type_code": "SUBCONTRACTING", "source_code": "GOOGLE",
     "source_key": "o-1", "expected_weekly_hours": 8},
    {"type": "opportunity", "organisation": {"name": "Reef Care"}, "opportunity_type_code": "SUBCONTRACTING",
     "source_code": "GOOGLE", "source_key": "o-2", "expected_weekly_hours": 8, "expected_hourly_rate": 60,
     "rate_basis": "QUOTED"},
]
WPM = 4.333333


@pytest.fixture
def kpi(conn, cfg, ingest):
    sync_config(conn, cfg)
    r = ingest(BOOK)
    assert r.failed == 0, r.errors
    return lambda: dict(conn.execute("SELECT * FROM pbi_kpi_current").fetchone())


def test_mrr_uses_approved_rates_only(kpi):
    k = kpi()
    assert k["mrr"] == pytest.approx(10 * 60 * WPM, abs=0.01)
    assert k["mrr_rosters_rate_unknown"] == 1
    assert k["weekly_billable_hours"] == 15  # hours are real even when the rate is not approved
    assert k["avg_billable_rate"] == 60  # same approved-rate basis as MRR
    assert k["projected_mrr"] == pytest.approx((10 * 60 + 4 * 65) * WPM, abs=0.01)
    assert (k["active_participants"], k["active_rosters"], k["scheduled_rosters"]) == (2, 2, 1)


def test_unknown_pipeline_value_is_not_zero_or_invented(conn, kpi):
    k = kpi()
    assert k["pipeline_monthly_value_known"] == pytest.approx(8 * 60 * WPM, abs=0.01)
    assert k["pipeline_value_unknown_count"] == 1
    row = conn.execute("SELECT est_monthly_value, value_status, est_monthly_value_display FROM pbi_fact_opportunity "
                       "WHERE source_detail IS NULL AND expected_hourly_rate IS NULL").fetchone()
    assert tuple(row) == (None, "UNKNOWN_RATE", "UNKNOWN")


def test_actual_revenue_and_pipeline_are_never_combined(conn, kpi):
    k = kpi()
    assert k["mrr"] < k["mrr"] + k["pipeline_monthly_value_known"]  # sanity: both non-trivial
    assert k["mrr"] == pytest.approx(10 * 60 * WPM, abs=0.01)  # pipeline not added in
    # No exported table carries a column that sums actual and estimated money.
    for (view,) in conn.execute("SELECT name FROM sqlite_master WHERE type = 'view' AND name LIKE 'pbi_%'"):
        cols = [r[1] for r in conn.execute(f"PRAGMA table_info({view})")]
        assert not [c for c in cols if "total_revenue_incl" in c or "combined" in c], view


def test_revenue_received_is_actual_only(conn, kpi, ingest):
    ingest([{"type": "revenue", "invoice_number": "INV-1", "invoice_date": "2026-09-20", "service_period_start":
             "2026-09-14", "service_period_end": "2026-09-20", "roster_ref": "R-1", "hours": 10, "hourly_rate": 60,
             "amount_ex_gst": 600, "status": "PAID", "paid_date": "2026-09-22"}])
    assert conn.execute("SELECT SUM(amount_received) FROM pbi_fact_revenue").fetchone()[0] == 600
    # Revenue without an invoice number / source key is refused (it could be double counted).
    r = ingest([{"type": "revenue", "invoice_date": "2026-09-21", "amount_ex_gst": 100}])
    assert r.failed == 1


def test_targets_come_from_config_and_keep_history(conn, cfg, kpi):
    assert kpi()["revenue_target"] == 5000
    assert kpi()["weekly_hours_target_min"] == 20
    cfg.raw["targets"]["mrr"] = 8000
    sync_config(conn, cfg)
    assert kpi()["revenue_target"] == 8000
    assert kpi()["revenue_gap"] == pytest.approx(8000 - 10 * 60 * WPM, abs=0.01)
    versions = conn.execute("SELECT target_value FROM business_targets WHERE target_code = 'MRR' "
                            "ORDER BY effective_from").fetchall()
    assert [v[0] for v in versions] == [5000, 8000]
