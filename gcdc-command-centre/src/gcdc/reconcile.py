"""Deterministic reconciliation run before every export. No AI involved.

1. Attach events that arrived without an opportunity to the organisation's
   primary opportunity (so the funnel counts every conversation once).
2. Move recorded opportunity stages forward to what the records prove.
3. Close follow-ups that were actually done (a later touch was sent).
4. Mark replies as responded when an outbound reply followed them.
5. Keep organisation relationship status in step with activity.
"""

from __future__ import annotations

import sqlite3

from gcdc.db import transaction
from gcdc.timeutil import now_iso


def reconcile(conn: sqlite3.Connection) -> dict[str, int]:
    now = now_iso()
    out: dict[str, int] = {}
    with transaction(conn):
        primary = "(SELECT primary_opportunity_id FROM v_org_primary_opportunity p WHERE p.organisation_id = {col})"
        for table, col in (("outreach", "organisation_id"), ("replies", "organisation_id"),
                           ("meetings", "organisation_id"), ("follow_ups", "organisation_id"),
                           ("participant_referrals", "referring_organisation_id")):
            cur = conn.execute(
                f"UPDATE {table} SET opportunity_id = {primary.format(col=f'{table}.{col}')}, updated_at = ? "
                f"WHERE opportunity_id IS NULL AND {col} IS NOT NULL "
                f"AND {primary.format(col=f'{table}.{col}')} IS NOT NULL", (now,))
            out[f"linked_{table}"] = cur.rowcount
        cur = conn.execute(
            "UPDATE trial_shifts SET opportunity_id = (SELECT p.opportunity_id FROM participant_referrals p "
            "WHERE p.referral_id = trial_shifts.referral_id), updated_at = ? "
            "WHERE opportunity_id IS NULL AND referral_id IS NOT NULL", (now,))
        out["linked_trial_shifts"] = cur.rowcount
        cur = conn.execute(
            "UPDATE rosters SET opportunity_id = COALESCE("
            " (SELECT p.opportunity_id FROM participant_referrals p WHERE p.referral_id = rosters.referral_id),"
            f" {primary.format(col='rosters.organisation_id')}), updated_at = ? "
            "WHERE opportunity_id IS NULL AND (referral_id IS NOT NULL OR organisation_id IS NOT NULL)", (now,))
        out["linked_rosters"] = cur.rowcount

        # 2. Stage catch-up (history rows are written by trigger).
        rows = conn.execute(
            "SELECT v.opportunity_id, s.stage_code FROM v_opportunity_status v "
            "JOIN ref_pipeline_stage s ON s.stage_order = v.evidence_stage_order "
            "WHERE v.evidence_stage_order > v.recorded_stage_order").fetchall()
        for opp_id, stage in rows:
            conn.execute("UPDATE opportunities SET stage_code = ?, stage_changed_at = ?, updated_at = ? "
                         "WHERE opportunity_id = ?", (stage, now, now, opp_id))
        out["stages_advanced"] = len(rows)

        # 3. Follow-ups satisfied by an outbound touch sent after the moment they became due-able.
        after = "COALESCE(follow_ups.satisfied_after, follow_ups.created_at)"
        # A call-back is only done by a call (an automated email to a warm lead is not a call-back).
        touch = (f"FROM outreach x WHERE x.organisation_id = follow_ups.organisation_id "
                 f"AND x.status IN ('SENT', 'BOUNCED') AND x.sent_at > {after} "
                 f"AND (follow_ups.action_type = 'OTHER' "
                 f"  OR (follow_ups.action_type = 'EMAIL' AND x.channel = 'EMAIL') "
                 f"  OR (follow_ups.action_type = 'CALL' AND x.channel IN ('PHONE', 'IN_PERSON', 'SMS')))")
        cur = conn.execute(
            f"UPDATE follow_ups SET status = 'DONE', completed_by = 'reconcile', updated_at = ?, "
            f" completed_at = (SELECT MIN(x.sent_at) {touch}), "
            f" result_outreach_id = (SELECT x.outreach_id {touch} ORDER BY x.sent_at LIMIT 1) "
            f"WHERE status = 'OPEN' AND action_type IN ('EMAIL', 'CALL', 'OTHER') AND EXISTS (SELECT 1 {touch})",
            (now,))
        out["follow_ups_closed"] = cur.rowcount

        # 4. Replies answered by a later outbound *reply* (automated sequence touches don't count).
        answered = ("FROM outreach x WHERE x.status = 'SENT' AND x.touch_type = 'REPLY' "
                    "AND x.sent_at > replies.received_at AND (x.thread_id = replies.thread_id "
                    "OR x.organisation_id = replies.organisation_id)")
        cur = conn.execute(
            f"UPDATE replies SET response_status = 'RESPONDED', updated_at = ?, "
            f" responded_at = (SELECT MIN(x.sent_at) {answered}), "
            f" response_outreach_id = (SELECT x.outreach_id {answered} ORDER BY x.sent_at LIMIT 1) "
            f"WHERE response_status = 'AWAITING_RESPONSE' AND EXISTS (SELECT 1 {answered})", (now,))
        out["replies_marked_responded"] = cur.rowcount

        # 5. Relationship status (never overrides DO_NOT_CONTACT / CLOSED).
        cur = conn.execute(
            "UPDATE organisations SET relationship_status = 'CLIENT', updated_at = ? "
            "WHERE relationship_status NOT IN ('CLIENT', 'DO_NOT_CONTACT', 'CLOSED') AND organisation_id IN ("
            " SELECT organisation_id FROM rosters WHERE status = 'ACTIVE' AND organisation_id IS NOT NULL)", (now,))
        out["orgs_to_client"] = cur.rowcount
        cur = conn.execute(
            "UPDATE organisations SET relationship_status = 'PARTNER', updated_at = ? "
            "WHERE relationship_status IN ('PROSPECT', 'IN_CONVERSATION') AND organisation_id IN ("
            " SELECT organisation_id FROM partnerships WHERE stage_code IN "
            " ('APPROVED', 'AWAITING_WORK', 'WORK_RECEIVED', 'RECURRING_REVENUE'))", (now,))
        out["orgs_to_partner"] = cur.rowcount
        cur = conn.execute(
            "UPDATE organisations SET relationship_status = 'IN_CONVERSATION', updated_at = ? "
            "WHERE relationship_status = 'PROSPECT' AND organisation_id IN (SELECT organisation_id FROM replies "
            " WHERE classification IN ('POSITIVE', 'REFERRAL', 'QUESTION', 'NEUTRAL'))", (now,))
        out["orgs_to_conversation"] = cur.rowcount
        cur = conn.execute(
            "UPDATE organisations SET relationship_status = 'DO_NOT_CONTACT', updated_at = ? "
            "WHERE relationship_status <> 'DO_NOT_CONTACT' AND organisation_id IN ("
            " SELECT organisation_id FROM suppression WHERE scope = 'ORGANISATION')", (now,))
        out["orgs_do_not_contact"] = cur.rowcount
    return {k: v for k, v in out.items() if v}
