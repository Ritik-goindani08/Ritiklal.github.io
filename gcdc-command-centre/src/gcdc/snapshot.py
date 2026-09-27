"""Daily metrics snapshots — append-only history for Power BI trends.

The morning refresh snapshots *yesterday* (a complete day). Activity counts
(emails sent, replies, referrals...) are exact for that Brisbane-local day;
state metrics (MRR, hours, open pipeline) are as at the end of that day, or
as at capture time for catch-up rows (flagged is_catch_up = 1). Existing rows
are never overwritten — the table rejects UPDATE and DELETE.
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, time, timedelta

from gcdc.db import pinned_now
from gcdc.timeutil import local_today, local_tz, now_utc, to_iso_utc

_ACTIVITY_SQL = """
SELECT
    (SELECT COUNT(*) FROM pbi_fact_opportunity WHERE discovered_date = c.today)                         AS opportunities_found,
    (SELECT COUNT(*) FROM pbi_fact_outreach WHERE is_attempted = 1 AND sent_date = c.today)             AS emails_sent,
    (SELECT COUNT(*) FROM pbi_fact_outreach WHERE is_attempted = 1 AND sent_date = c.today
       AND touch_type = 'FIRST_TOUCH')                                                                  AS first_touch_sent,
    (SELECT COUNT(*) FROM pbi_fact_outreach WHERE is_attempted = 1 AND sent_date = c.today
       AND touch_type = 'FOLLOW_UP')                                                                    AS followups_sent,
    (SELECT COUNT(*) FROM pbi_fact_outreach WHERE bounced_date = c.today)                               AS emails_bounced,
    (SELECT COUNT(*) FROM pbi_fact_reply WHERE is_human_reply = 1 AND is_estimated_date = 0
       AND received_date = c.today)                                                                     AS replies,
    (SELECT COUNT(*) FROM pbi_fact_reply WHERE is_positive = 1 AND is_estimated_date = 0
       AND received_date = c.today)                                                                     AS positive_replies,
    (SELECT COUNT(*) FROM pbi_fact_meeting WHERE is_booked_or_held = 1 AND booked_date = c.today)       AS meetings_booked,
    (SELECT COUNT(*) FROM pbi_fact_meeting WHERE is_held = 1 AND meeting_date = c.today)                AS meetings_held,
    (SELECT COUNT(*) FROM pbi_fact_referral WHERE received_date = c.today)                              AS referrals,
    (SELECT COUNT(*) FROM pbi_fact_trial_shift WHERE is_scheduled_or_done = 1 AND trial_date = c.today) AS trial_shifts,
    ROUND(COALESCE((SELECT SUM(amount_received) FROM pbi_fact_revenue WHERE paid_date = c.today), 0), 2) AS revenue_received_day
FROM v_ctx c
"""


def _end_of_local_day(d: date, offset_hours: float) -> datetime:
    return datetime.combine(d, time(23, 59, 59), tzinfo=local_tz(offset_hours))


def take_snapshot(conn: sqlite3.Connection, snapshot_date: date, *, offset_hours: float,
                  catch_up: bool = False) -> bool:
    """Write one snapshot row. Returns False if that date already exists."""
    if conn.execute("SELECT 1 FROM daily_metrics_snapshot WHERE snapshot_date = ?",
                    (snapshot_date.isoformat(),)).fetchone():
        return False
    today = local_today(offset_hours)
    if snapshot_date > today:
        raise ValueError("cannot snapshot a future date")
    # State as at end of the day (or now, if the day is still in progress / a catch-up).
    as_of = _end_of_local_day(snapshot_date, offset_hours)
    if as_of > now_utc():
        as_of = now_utc()
    with pinned_now(conn, as_of, offset_hours):
        k = dict(conn.execute("SELECT * FROM pbi_kpi_current").fetchone())
        a = dict(conn.execute(_ACTIVITY_SQL).fetchone())
    row = {
        "snapshot_date": snapshot_date.isoformat(),
        "captured_at_utc": to_iso_utc(now_utc()),
        "is_catch_up": int(catch_up),
        "mrr": k["mrr"],
        "mrr_rosters_rate_unknown": k["mrr_rosters_rate_unknown"],
        "weekly_billable_hours": k["weekly_billable_hours"],
        "avg_billable_rate": k["avg_billable_rate"],
        "revenue_received_day": a["revenue_received_day"],
        "revenue_received_mtd": k["revenue_received_mtd"],
        "revenue_invoiced_mtd": k["revenue_invoiced_mtd"],
        "revenue_target": k["revenue_target"],
        "revenue_gap": k["revenue_gap"],
        "weekly_hours_target_min": k["weekly_hours_target_min"],
        "active_participants": k["active_participants"],
        "active_rosters": k["active_rosters"],
        "active_opportunities": k["active_opportunities"],
        "p1_count": k["p1_open"],
        "p2_count": k["p2_open"],
        "p3_count": k["p3_open"],
        "pipeline_monthly_value_known": k["pipeline_monthly_value_known"],
        "pipeline_value_unknown_count": k["pipeline_value_unknown_count"],
        "near_term_monthly_value_known": k["near_term_monthly_value_known"],
        "weighted_pipeline_monthly": k["weighted_pipeline_monthly"],
        "opportunities_found": a["opportunities_found"],
        "emails_sent": a["emails_sent"],
        "first_touch_sent": a["first_touch_sent"],
        "followups_sent": a["followups_sent"],
        "emails_bounced": a["emails_bounced"],
        "replies": a["replies"],
        "positive_replies": a["positive_replies"],
        "meetings_booked": a["meetings_booked"],
        "meetings_held": a["meetings_held"],
        "referrals": a["referrals"],
        "trial_shifts": a["trial_shifts"],
        "orgs_contacted_total": k["orgs_contacted"],
        "overdue_followups": k["followups_overdue"],
        "replies_awaiting_response": k["replies_awaiting_response"],
        "workers_active": k["workers_active"],
        "worker_available_hours": k["worker_available_hours"],
        "worker_allocated_hours": k["worker_allocated_hours"],
        "open_worker_requirements": k["open_worker_requirements"],
        "dq_errors": k["dq_errors"],
        "agents_red": k["agents_red"],
    }
    cols = ", ".join(row)
    marks = ", ".join("?" for _ in row)
    cur = conn.execute(f"INSERT INTO daily_metrics_snapshot ({cols}) VALUES ({marks}) "
                       f"ON CONFLICT(snapshot_date) DO NOTHING", tuple(row.values()))
    return cur.rowcount == 1


def snapshot_catch_up(conn: sqlite3.Connection, *, offset_hours: float, max_days: int = 14,
                      through: date | None = None) -> list[str]:
    """Snapshot every complete day missing since the last snapshot (default: through yesterday)."""
    through = through or (local_today(offset_hours) - timedelta(days=1))
    last = conn.execute("SELECT MAX(snapshot_date) FROM daily_metrics_snapshot").fetchone()[0]
    start = date.fromisoformat(last) + timedelta(days=1) if last else through
    start = max(start, through - timedelta(days=max_days - 1))
    written = []
    d = start
    while d <= through:
        # Only the most recent day's state is genuinely end-of-day; older gaps are catch-ups.
        if take_snapshot(conn, d, offset_hours=offset_hours, catch_up=(d != through)):
            written.append(d.isoformat())
        d += timedelta(days=1)
    return written
