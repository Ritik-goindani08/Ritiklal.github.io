"""Automated data-quality checks. Results are written to data_quality_results
(exported to Power BI as pbi_fact_data_quality); ERROR checks also raise alerts.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from gcdc.ingest import raise_alert
from gcdc.timeutil import now_iso


@dataclass(frozen=True)
class Check:
    code: str
    name: str
    severity: str  # ERROR | WARNING | INFO
    sql: str       # must return entity_type, entity_id, organisation_id, detail


CHECKS: tuple[Check, ...] = (
    Check("DUP_ORG_NAME", "Possible duplicate organisations (same name)", "WARNING", """
        SELECT 'organisations', o.organisation_id, o.organisation_id,
               'Same name as organisation ' || d.organisation_id || ' (' || d.name || ')'
        FROM organisations o
        JOIN organisations d ON d.name_key = o.name_key AND d.organisation_id < o.organisation_id
        WHERE o.merged_into_id IS NULL AND d.merged_into_id IS NULL
          AND NOT (o.abn IS NOT NULL AND d.abn IS NOT NULL AND o.abn <> d.abn)"""),
    Check("DUP_ORG_ABN", "Duplicate organisations (same ABN)", "ERROR", """
        SELECT 'organisations', o.organisation_id, o.organisation_id,
               'Same ABN ' || o.abn || ' as organisation ' || d.organisation_id || ' (' || d.name || ')'
        FROM organisations o
        JOIN organisations d ON d.abn = o.abn AND d.organisation_id < o.organisation_id
        WHERE o.merged_into_id IS NULL AND d.merged_into_id IS NULL"""),
    Check("DUP_ORG_DOMAIN", "Possible duplicate organisations (same website domain)", "WARNING", """
        SELECT 'organisations', o.organisation_id, o.organisation_id,
               'Same domain ' || o.domain || ' as organisation ' || d.organisation_id || ' (' || d.name || ')'
        FROM organisations o
        JOIN organisations d ON d.domain = o.domain AND d.organisation_id < o.organisation_id
        WHERE o.merged_into_id IS NULL AND d.merged_into_id IS NULL AND o.domain IS NOT NULL
          AND NOT (o.abn IS NOT NULL AND d.abn IS NOT NULL AND o.abn <> d.abn)"""),
    Check("MISSING_ORG_LINK", "Records not linked to a live organisation", "ERROR", """
        SELECT 'participant_referrals', referral_id, NULL, 'Referral ' || participant_ref || ' has no referring organisation'
          FROM participant_referrals WHERE referring_organisation_id IS NULL
        UNION ALL
        SELECT 'revenue_transactions', revenue_id, NULL, 'Invoice ' || COALESCE(invoice_number, revenue_id)
               || ' cannot be attributed to any organisation'
          FROM v_revenue_attributed WHERE attributed_organisation_id IS NULL
        UNION ALL
        SELECT t.tbl, t.id, t.org, 'Linked to merged organisation ' || t.org FROM (
            SELECT 'opportunities' AS tbl, opportunity_id AS id, organisation_id AS org FROM opportunities
            UNION ALL SELECT 'outreach', outreach_id, organisation_id FROM outreach
            UNION ALL SELECT 'replies', reply_id, organisation_id FROM replies
            UNION ALL SELECT 'contacts', contact_id, organisation_id FROM contacts
        ) t JOIN organisations o ON o.organisation_id = t.org WHERE o.merged_into_id IS NOT NULL"""),
    Check("OPP_NO_SOURCE", "Opportunities without a source", "WARNING", """
        SELECT 'opportunities', opportunity_id, organisation_id, 'No source recorded: ' || title
        FROM opportunities
        WHERE (source_code = 'OTHER' AND source_detail IS NULL AND source_url IS NULL)"""),
    Check("UNVERIFIED_EMAIL_SCHEDULED", "Outreach scheduled to an unverified or bad email", "WARNING", """
        SELECT 'outreach', x.outreach_id, x.organisation_id,
               'Scheduled to ' || x.to_email || ' whose email status is ' || c.email_status
        FROM outreach x JOIN contacts c ON c.contact_id = x.contact_id
        WHERE x.status IN ('SCHEDULED', 'APPROVED', 'PENDING_APPROVAL')
          AND c.email_status IN ('UNVERIFIED', 'RISKY', 'INVALID', 'BOUNCED')"""),
    Check("UNVERIFIED_EMAILS", "Contacts with unverified emails (summary)", "INFO", """
        SELECT 'contacts', NULL, NULL, n || ' contact emails are not yet verified by the Email Verification agent'
        FROM (SELECT COUNT(*) AS n FROM contacts WHERE email IS NOT NULL AND email_status = 'UNVERIFIED') WHERE n > 0"""),
    Check("STAGE_AHEAD_OF_EVIDENCE", "Opportunity stage not supported by records", "WARNING", """
        SELECT 'opportunities', v.opportunity_id, v.organisation_id,
               'Stage is ' || o.stage_code || ' but records only prove ' ||
               (SELECT stage_code FROM ref_pipeline_stage WHERE stage_order = v.evidence_stage_order)
        FROM v_opportunity_status v JOIN opportunities o ON o.opportunity_id = v.opportunity_id
        WHERE v.recorded_stage_order > v.evidence_stage_order + 1 AND v.recorded_stage_order >= 4"""),
    Check("INVALID_STAGE", "Invalid opportunity stage or status combination", "ERROR", """
        SELECT 'opportunities', o.opportunity_id, o.organisation_id, 'Unknown stage ' || o.stage_code
          FROM opportunities o LEFT JOIN ref_pipeline_stage s ON s.stage_code = o.stage_code WHERE s.stage_code IS NULL
        UNION ALL
        SELECT 'opportunities', o.opportunity_id, o.organisation_id, 'Marked WON but has no roster'
          FROM opportunities o WHERE o.status = 'WON'
           AND NOT EXISTS (SELECT 1 FROM rosters r WHERE r.opportunity_id = o.opportunity_id)"""),
    Check("REVENUE_NO_SOURCE", "Revenue without a source record", "ERROR", """
        SELECT 'revenue_transactions', revenue_id, organisation_id,
               'Invoice ' || COALESCE(invoice_number, source_key) || ' is not linked to a roster, participant or organisation'
        FROM revenue_transactions
        WHERE roster_id IS NULL AND participant_id IS NULL AND organisation_id IS NULL
        UNION ALL
        SELECT 'revenue_transactions', revenue_id, organisation_id,
               'Invoice ' || COALESCE(invoice_number, source_key) || ' amount is zero or negative'
        FROM revenue_transactions WHERE amount_ex_gst <= 0 AND status NOT IN ('VOID', 'WRITTEN_OFF')"""),
    Check("PARTICIPANT_NO_ROSTER", "Active participant without a roster", "ERROR", """
        SELECT 'participants', p.participant_id, p.referring_organisation_id,
               'Participant ' || p.participant_ref || ' is ACTIVE but has no active or scheduled roster'
        FROM participants p
        WHERE p.status = 'ACTIVE' AND NOT EXISTS (
            SELECT 1 FROM rosters r WHERE r.participant_id = p.participant_id AND r.status IN ('ACTIVE', 'SCHEDULED'))"""),
    Check("ROSTER_RATE_UNKNOWN", "Active roster without an approved rate (excluded from MRR)", "WARNING", """
        SELECT 'rosters', r.roster_id, r.organisation_id,
               'Roster ' || COALESCE(r.roster_ref, r.roster_id) || ' has ' || COALESCE(r.weekly_hours, '?')
               || ' h/wk but rate is ' || COALESCE(r.rate_basis || ' $' || r.hourly_rate, 'UNKNOWN')
        FROM rosters r JOIN v_roster_value v ON v.roster_id = r.roster_id
        WHERE v.active_rate_not_approved = 1"""),
    Check("IMPOSSIBLE_DATES", "Impossible or implausible dates", "ERROR", """
        SELECT 'outreach', outreach_id, organisation_id, 'Sent in the future: ' || sent_at
          FROM outreach, v_ctx c WHERE status = 'SENT' AND sent_at > datetime(c.now_utc, '+1 hour')
        UNION ALL
        SELECT 'replies', reply_id, organisation_id, 'Received in the future: ' || received_at
          FROM replies, v_ctx c WHERE received_at > datetime(c.now_utc, '+1 hour')
        UNION ALL
        SELECT 'opportunities', opportunity_id, organisation_id, 'Discovered in the future: ' || discovered_at
          FROM opportunities, v_ctx c WHERE discovered_at > datetime(c.now_utc, '+1 hour')
        UNION ALL
        SELECT t.tbl, t.id, t.org, t.what || ' ' || t.d || ' is before records start ' || s.start_date FROM (
            SELECT 'outreach' AS tbl, outreach_id AS id, organisation_id AS org, 'Sent' AS what, sent_at AS d FROM outreach
            UNION ALL SELECT 'outreach', outreach_id, organisation_id, 'Scheduled', scheduled_for FROM outreach
            UNION ALL SELECT 'replies', reply_id, organisation_id, 'Received', received_at FROM replies
            UNION ALL SELECT 'follow_ups', follow_up_id, organisation_id, 'Due', due_date FROM follow_ups
            UNION ALL SELECT 'revenue_transactions', revenue_id, organisation_id, 'Invoiced', invoice_date
                      FROM revenue_transactions
        ) t CROSS JOIN (SELECT COALESCE((SELECT value FROM settings WHERE key = 'business.records_start_date'),
                                        '2026-01-01') AS start_date) s
        WHERE t.d IS NOT NULL AND t.d < s.start_date
        UNION ALL
        SELECT 'rosters', roster_id, organisation_id, 'Ends ' || end_date || ' before it starts ' || start_date
          FROM rosters WHERE end_date < start_date
        UNION ALL
        SELECT 'participants', participant_id, referring_organisation_id, 'Ends before it starts'
          FROM participants WHERE end_date < start_date
        UNION ALL
        SELECT 'revenue_transactions', revenue_id, organisation_id, 'Paid ' || paid_date || ' before invoiced ' || invoice_date
          FROM revenue_transactions WHERE paid_date < invoice_date
        UNION ALL
        SELECT 'revenue_transactions', revenue_id, organisation_id, 'Invoice dated in the future: ' || invoice_date
          FROM revenue_transactions, v_ctx c WHERE invoice_date > date(c.today, '+1 day')
        UNION ALL
        SELECT 'meetings', meeting_id, organisation_id, 'Marked COMPLETED but scheduled in the future'
          FROM meetings, v_ctx c WHERE status = 'COMPLETED' AND scheduled_start > c.now_utc"""),
    Check("DUP_OUTREACH", "Duplicate outreach records", "WARNING", """
        SELECT 'outreach', x.outreach_id, x.organisation_id,
               'Same recipient, subject and day as outreach ' || y.outreach_id
        FROM outreach x JOIN outreach y
          ON y.to_email = x.to_email AND COALESCE(y.subject, '') = COALESCE(x.subject, '')
         AND date(COALESCE(y.sent_at, y.scheduled_for)) = date(COALESCE(x.sent_at, x.scheduled_for))
         AND y.channel = x.channel AND y.outreach_id < x.outreach_id
        WHERE x.status NOT IN ('CANCELLED') AND y.status NOT IN ('CANCELLED') AND x.to_email IS NOT NULL"""),
    Check("SENT_TO_SUPPRESSED", "Outreach sent to a suppressed address", "ERROR", """
        SELECT 'outreach', x.outreach_id, x.organisation_id, 'Sent to suppressed ' || COALESCE(x.to_email, 'organisation')
        FROM outreach x JOIN suppression s ON
             ((s.scope = 'EMAIL' AND s.email = x.to_email) OR (s.scope = 'ORGANISATION' AND s.organisation_id = x.organisation_id))
        WHERE x.status = 'SENT' AND x.sent_at > s.created_at"""),
    Check("SENT_UNCONFIRMED", "Outreach marked sent from a plan sheet, not confirmed by the mailbox", "INFO", """
        SELECT 'outreach', NULL, NULL, n || ' outreach records were imported as sent from planning sheets; '
               || 'run the mailbox sync to confirm them'
        FROM (SELECT COUNT(*) AS n FROM outreach WHERE status IN ('SENT', 'BOUNCED') AND evidence = 'SHEET_PLAN')
        WHERE n > 0"""),
    Check("WORKER_NOT_VERIFIED", "Active worker missing verification", "ERROR", """
        SELECT 'workers', w.worker_id, NULL, w.worker_ref || ' ' || w.display_name || ' is ACTIVE but missing: '
               || v.verification_gaps
        FROM workers w JOIN v_worker_verified v ON v.worker_id = w.worker_id
        WHERE w.status = 'ACTIVE' AND v.is_verified_active = 0"""),
    Check("ROSTER_UNVERIFIED_WORKER", "Roster assigned to an unverified worker", "ERROR", """
        SELECT 'rosters', r.roster_id, r.organisation_id,
               'Roster ' || COALESCE(r.roster_ref, r.roster_id) || ' uses unverified worker ' || w.worker_ref
        FROM rosters r JOIN workers w ON w.worker_id = r.primary_worker_id
        JOIN v_worker_verified v ON v.worker_id = w.worker_id
        WHERE r.status IN ('ACTIVE', 'SCHEDULED') AND v.is_verified_active = 0"""),
    Check("WORKER_CHECK_EXPIRING", "Worker check expiring within 30 days", "WARNING", """
        SELECT 'workers', w.worker_id, NULL, w.worker_ref || ' ' || t.what || ' expires ' || t.d
        FROM workers w CROSS JOIN v_ctx c JOIN (
            SELECT worker_id, 'NDIS screening' AS what, ndis_screening_expiry AS d FROM workers
            UNION ALL SELECT worker_id, 'First aid', first_aid_expiry FROM workers
            UNION ALL SELECT worker_id, 'CPR', cpr_expiry FROM workers
            UNION ALL SELECT worker_id, 'Blue card', blue_card_expiry FROM workers
        ) t ON t.worker_id = w.worker_id
        WHERE w.status = 'ACTIVE' AND t.d BETWEEN c.today AND date(c.today, '+30 days')"""),
    Check("AGENCY_MANAGED_REFERRAL", "Referral GCDC cannot bill (NDIA-managed)", "WARNING", """
        SELECT 'participant_referrals', p.referral_id, p.referring_organisation_id,
               'Referral ' || p.participant_ref || ' is NDIA-managed; GCDC is not a registered NDIS provider'
        FROM participant_referrals p CROSS JOIN v_cfg cfg
        WHERE p.funding_type = 'NDIS_AGENCY_MANAGED' AND cfg.registered_ndis_provider = 0
          AND p.status NOT IN ('DECLINED', 'LOST', 'WITHDRAWN')"""),
    Check("ORG_UNKNOWN_REGION", "Contacted organisations with unknown region", "INFO", """
        SELECT 'organisations', NULL, NULL, n || ' contacted organisations have no region'
        FROM (SELECT COUNT(*) AS n FROM organisations o JOIN v_org_activity a ON a.organisation_id = o.organisation_id
              WHERE a.is_contacted = 1 AND o.region_code = 'UNKNOWN') WHERE n > 0"""),
)


def run_checks(conn: sqlite3.Connection) -> dict:
    now = now_iso()
    results: list[tuple] = []
    by_check: dict[str, int] = {}
    for check in CHECKS:
        rows = conn.execute(check.sql).fetchall()
        by_check[check.code] = len(rows)
        results.extend((check.code, check.severity, *row) for row in rows)
    errors = sum(1 for r in results if r[1] == "ERROR")
    warnings = sum(1 for r in results if r[1] == "WARNING")
    conn.execute("BEGIN IMMEDIATE")
    try:
        run_id = conn.execute(
            "INSERT INTO data_quality_runs (run_at, checks_run, issues_total, errors_total, warnings_total) "
            "VALUES (?, ?, ?, ?, ?)", (now, len(CHECKS), len(results), errors, warnings)).lastrowid
        conn.executemany(
            "INSERT INTO data_quality_results (check_run_id, check_code, severity, entity_type, entity_id, "
            "organisation_id, detail) VALUES (?, ?, ?, ?, ?, ?, ?)",
            [(run_id, *r) for r in results])
        # Keep 60 days of detail history.
        conn.execute("DELETE FROM data_quality_results WHERE check_run_id IN (SELECT check_run_id FROM "
                     "data_quality_runs WHERE run_at < datetime('now', '-60 days'))")
        for check in CHECKS:
            key = f"dq-{check.code}"
            n = by_check[check.code]
            if check.severity == "ERROR" and n:
                raise_alert(conn, key=key, source_type="DATA_QUALITY", severity="ERROR", category="DATA_QUALITY",
                            message=f"{check.name}: {n} record(s)")
            elif n == 0:
                conn.execute("UPDATE alerts SET status = 'RESOLVED', resolved_at = ?, resolved_by = 'auto' "
                             "WHERE alert_key = ? AND status <> 'RESOLVED'", (now, key))
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")
    return {"check_run_id": run_id, "issues": len(results), "errors": errors, "warnings": warnings,
            "by_check": {k: v for k, v in by_check.items() if v}}


CHECK_NAMES = {c.code: c.name for c in CHECKS}
