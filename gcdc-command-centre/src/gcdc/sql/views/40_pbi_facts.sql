-- =============================================================================
-- 40: Power BI facts. Business rules are applied here; DAX only aggregates the
-- flags and values below. Dates are Brisbane-local 'YYYY-MM-DD'.
-- Actual revenue (pbi_fact_revenue, pbi_fact_roster) and estimated pipeline
-- value (pbi_fact_opportunity, pbi_fact_referral) are separate columns in
-- separate tables and are never summed together.
-- =============================================================================

CREATE VIEW pbi_fact_opportunity AS
SELECT
    o.opportunity_id,
    o.organisation_id,
    o.contact_id,
    o.title,
    o.opportunity_type_code,
    o.source_code,
    o.source_detail,
    o.source_url,
    COALESCE(NULLIF(o.region_code, 'UNKNOWN'), org.region_code)                         AS region_code,
    o.service_type_code,
    COALESCE(o.priority, 'Unassigned')                                                  AS priority,
    CASE o.priority WHEN 'P1' THEN 1 WHEN 'P2' THEN 2 WHEN 'P3' THEN 3 ELSE 4 END        AS priority_sort,
    o.score,
    o.stage_code                                                                        AS recorded_stage_code,
    v.effective_stage_code                                                              AS stage_code,
    st.stage_name,
    v.stage_order_reached,
    o.status,
    v.is_active,
    o.is_verified,
    date(o.discovered_at, c.tz_mod)                                                     AS discovered_date,
    o.discovered_by,
    CAST(julianday(c.today) - julianday(date(o.discovered_at, c.tz_mod)) AS INTEGER)    AS age_days,
    date(v.last_activity_at, c.tz_mod)                                                  AS last_activity_date,
    CAST(julianday(c.today) - julianday(date(v.last_activity_at, c.tz_mod)) AS INTEGER) AS days_since_activity,
    o.next_action,
    o.next_action_due,
    o.owner,
    o.requires_approval,
    v.expected_weekly_hours,
    v.expected_hourly_rate,
    v.rate_basis,
    v.value_basis,
    v.est_weekly_value,
    v.est_monthly_value,
    v.value_status,
    CASE WHEN v.est_weekly_value IS NULL THEN 'UNKNOWN'
         ELSE '$' || printf('%,d', CAST(ROUND(v.est_weekly_value) AS INTEGER)) END      AS est_weekly_value_display,
    CASE WHEN v.est_monthly_value IS NULL THEN 'UNKNOWN'
         ELSE '$' || printf('%,d', CAST(ROUND(v.est_monthly_value) AS INTEGER)) END     AS est_monthly_value_display,
    v.stage_probability,
    CASE WHEN v.est_monthly_value IS NOT NULL AND v.stage_probability IS NOT NULL
         THEN ROUND(v.est_monthly_value * v.stage_probability, 2) END                   AS weighted_monthly_value,
    v.pipeline_horizon,
    COALESCE(o.participant_ref, v.referral_participant_ref)                             AS participant_ref,
    CASE WHEN ABS(julianday(o.discovered_at) - julianday(org.first_seen_at)) < 1 THEN 1 ELSE 0 END AS is_new_organisation,
    CASE WHEN date(o.discovered_at, c.tz_mod) = c.today THEN 1 ELSE 0 END               AS found_today,
    CASE WHEN date(o.discovered_at, c.tz_mod) >= c.week_start THEN 1 ELSE 0 END         AS found_this_week,
    CASE WHEN date(o.discovered_at, c.tz_mod) >= c.month_start THEN 1 ELSE 0 END        AS found_this_month,
    1                                                                                   AS reached_found,
    CASE WHEN v.stage_order_reached >= 2 THEN 1 ELSE 0 END                              AS reached_verified,
    CASE WHEN v.stage_order_reached >= 3 THEN 1 ELSE 0 END                              AS reached_contacted,
    CASE WHEN v.stage_order_reached >= 4 THEN 1 ELSE 0 END                              AS reached_replied,
    CASE WHEN v.stage_order_reached >= 5 THEN 1 ELSE 0 END                              AS reached_positive,
    CASE WHEN v.stage_order_reached >= 6 THEN 1 ELSE 0 END                              AS reached_meeting,
    CASE WHEN v.stage_order_reached >= 7 THEN 1 ELSE 0 END                              AS reached_referral,
    CASE WHEN v.stage_order_reached >= 8 THEN 1 ELSE 0 END                              AS reached_trial,
    CASE WHEN v.stage_order_reached >= 9 THEN 1 ELSE 0 END                              AS reached_roster,
    COALESCE((SELECT SUM(r.recognised_amount) FROM v_revenue_attributed r
               WHERE r.attributed_opportunity_id = o.opportunity_id), 0)                AS actual_revenue_to_date,
    COALESCE((SELECT SUM(rv.weekly_hours) FROM v_roster_value rv
               WHERE rv.attributed_opportunity_id = o.opportunity_id AND rv.status = 'ACTIVE'), 0)
                                                                                        AS active_roster_weekly_hours
FROM opportunities o
JOIN v_opportunity_value v ON v.opportunity_id = o.opportunity_id
JOIN organisations org ON org.organisation_id = o.organisation_id
JOIN ref_pipeline_stage st ON st.stage_code = v.effective_stage_code
CROSS JOIN v_ctx c;

CREATE VIEW pbi_fact_contact AS
SELECT
    ct.contact_id,
    ct.organisation_id,
    org.region_code,
    ct.full_name                                                                        AS contact_name,
    ct.role_title,
    ct.email,
    ct.email_status,
    COALESCE(ct.email_provenance, 'UNKNOWN')                                            AS email_provenance,
    ct.is_decision_maker,
    COALESCE(ct.is_role_address, 0)                                                     AS is_role_address,
    CASE WHEN ct.email_status = 'VERIFIED' THEN 1 ELSE 0 END                            AS is_verified_email,
    CASE WHEN ct.email_status IN ('INVALID', 'BOUNCED') THEN 1 ELSE 0 END               AS is_invalid_email,
    date(ct.created_at, c.tz_mod)                                                       AS created_date
FROM contacts ct
JOIN organisations org ON org.organisation_id = ct.organisation_id
CROSS JOIN v_ctx c;

CREATE VIEW pbi_fact_stage_history AS
SELECT
    h.history_id,
    h.opportunity_id,
    h.from_stage,
    h.to_stage,
    date(h.changed_at, c.tz_mod) AS changed_date,
    h.changed_by
FROM opportunity_stage_history h
CROSS JOIN v_ctx c;

CREATE VIEW pbi_fact_outreach AS
SELECT
    x.outreach_id,
    x.organisation_id,
    x.contact_id,
    x.opportunity_id,
    org.region_code,
    x.channel,
    x.touch_type,
    x.sequence_step,
    x.campaign_code,
    x.status,
    x.evidence,
    x.to_email,
    ct.full_name                                                                        AS contact_name,
    ct.email_status                                                                     AS contact_email_status,
    x.subject,
    date(x.scheduled_for, c.tz_mod)                                                     AS scheduled_date,
    strftime('%H:%M', x.scheduled_for, c.tz_mod)                                        AS scheduled_time,
    date(x.sent_at, c.tz_mod)                                                           AS sent_date,
    date(x.bounced_at, c.tz_mod)                                                        AS bounced_date,
    COALESCE(date(x.sent_at, c.tz_mod), date(x.scheduled_for, c.tz_mod), date(x.created_at, c.tz_mod)) AS activity_date,
    CASE WHEN x.status IN ('SENT', 'BOUNCED') AND x.sent_at IS NOT NULL THEN 1 ELSE 0 END  AS is_attempted,
    CASE WHEN x.status = 'SENT' THEN 1 ELSE 0 END                                       AS is_sent,
    CASE WHEN x.status = 'SENT' AND x.touch_type = 'FIRST_TOUCH' THEN 1 ELSE 0 END      AS is_first_touch_sent,
    CASE WHEN x.status = 'SENT' AND x.touch_type = 'FOLLOW_UP' THEN 1 ELSE 0 END        AS is_follow_up_sent,
    CASE WHEN x.status = 'BOUNCED' THEN 1 ELSE 0 END                                    AS is_bounced,
    CASE WHEN x.status = 'SCHEDULED' THEN 1 ELSE 0 END                                  AS is_scheduled,
    CASE WHEN x.status IN ('SCHEDULED', 'APPROVED') AND date(x.scheduled_for, c.tz_mod) = c.today THEN 1 ELSE 0 END
                                                                                        AS is_scheduled_today,
    CASE WHEN x.status = 'SCHEDULED' AND x.touch_type = 'FOLLOW_UP' AND date(x.scheduled_for, c.tz_mod) = c.today
         THEN 1 ELSE 0 END                                                              AS is_follow_up_scheduled_today,
    CASE WHEN x.status = 'CANCELLED' THEN 1 ELSE 0 END                                  AS is_cancelled,
    CASE WHEN x.status = 'PENDING_APPROVAL' OR (x.requires_approval = 1 AND x.approved_at IS NULL
          AND x.status IN ('DRAFT', 'PENDING_APPROVAL')) THEN 1 ELSE 0 END              AS is_pending_approval
FROM outreach x
JOIN organisations org ON org.organisation_id = x.organisation_id
LEFT JOIN contacts ct ON ct.contact_id = x.contact_id
CROSS JOIN v_ctx c;

CREATE VIEW pbi_fact_reply AS
SELECT
    r.reply_id,
    r.organisation_id,
    r.contact_id,
    r.opportunity_id,
    r.outreach_id,
    org.region_code,
    r.channel,
    date(r.received_at, c.tz_mod)                                                       AS received_date,
    datetime(r.received_at, c.tz_mod)                                                   AS received_at_local,
    r.received_at_is_estimate                                                           AS is_estimated_date,
    r.classification,
    CASE WHEN r.classification NOT IN ('OUT_OF_OFFICE', 'AUTO_REPLY') THEN 1 ELSE 0 END AS is_human_reply,
    CASE WHEN r.classification IN ('POSITIVE', 'REFERRAL') THEN 1 ELSE 0 END            AS is_positive,
    r.requires_response,
    r.response_status,
    CASE WHEN r.response_status = 'AWAITING_RESPONSE' AND r.requires_response = 1 THEN 1 ELSE 0 END AS is_awaiting_response,
    CASE WHEN r.response_status = 'AWAITING_RESPONSE' AND r.requires_response = 1
         THEN ROUND((julianday(c.now_utc) - julianday(r.received_at)) * 24, 1) END      AS hours_waiting,
    CASE WHEN r.response_status = 'AWAITING_RESPONSE' AND r.requires_response = 1
          AND (julianday(c.now_utc) - julianday(r.received_at)) * 24 > cfg.reply_response_hours THEN 1 ELSE 0 END
                                                                                        AS is_response_overdue,
    date(r.responded_at, c.tz_mod)                                                      AS responded_date,
    r.from_email,
    r.subject,
    r.summary
FROM replies r
JOIN organisations org ON org.organisation_id = r.organisation_id
CROSS JOIN v_ctx c
CROSS JOIN v_cfg cfg;

CREATE VIEW pbi_fact_meeting AS
SELECT
    m.meeting_id,
    m.organisation_id,
    m.contact_id,
    m.opportunity_id,
    org.region_code,
    m.meeting_type,
    date(m.scheduled_start, c.tz_mod)                                                   AS meeting_date,
    strftime('%H:%M', m.scheduled_start, c.tz_mod)                                      AS meeting_time,
    m.status,
    m.mode,
    m.location,
    date(COALESCE(m.booked_at, m.created_at), c.tz_mod)                                 AS booked_date,
    CASE WHEN m.status IN ('BOOKED', 'COMPLETED', 'RESCHEDULED') THEN 1 ELSE 0 END      AS is_booked_or_held,
    CASE WHEN m.status = 'COMPLETED' THEN 1 ELSE 0 END                                  AS is_held,
    CASE WHEN m.status IN ('BOOKED', 'RESCHEDULED') AND date(m.scheduled_start, c.tz_mod) = c.today THEN 1 ELSE 0 END
                                                                                        AS is_today,
    CASE WHEN m.status IN ('BOOKED', 'RESCHEDULED') AND date(m.scheduled_start, c.tz_mod) >= c.today
          AND date(m.scheduled_start, c.tz_mod) <= date(c.today, '+' || cfg.upcoming_meeting_days || ' days')
         THEN 1 ELSE 0 END                                                              AS is_upcoming,
    m.outcome,
    m.next_step
FROM meetings m
JOIN organisations org ON org.organisation_id = m.organisation_id
CROSS JOIN v_ctx c
CROSS JOIN v_cfg cfg;

CREATE VIEW pbi_fact_follow_up AS
SELECT
    f.follow_up_id,
    f.organisation_id,
    f.opportunity_id,
    f.contact_id,
    org.region_code,
    f.action_type,
    f.description,
    f.due_date,
    f.status,
    CASE WHEN f.status <> 'OPEN' THEN f.status
         WHEN f.due_date < c.today THEN 'OVERDUE'
         WHEN f.due_date = c.today THEN 'DUE_TODAY'
         ELSE 'UPCOMING' END                                                            AS due_status,
    CASE WHEN f.status = 'OPEN' AND f.due_date < c.today
         THEN CAST(julianday(c.today) - julianday(f.due_date) AS INTEGER) ELSE 0 END    AS days_overdue,
    CASE WHEN f.status = 'OPEN' AND f.due_date < c.today THEN 1 ELSE 0 END              AS is_overdue,
    CASE WHEN f.status = 'OPEN' AND f.due_date = c.today THEN 1 ELSE 0 END              AS is_due_today,
    f.owner,
    date(f.completed_at, c.tz_mod)                                                      AS completed_date
FROM follow_ups f
JOIN organisations org ON org.organisation_id = f.organisation_id
CROSS JOIN v_ctx c;

CREATE VIEW pbi_fact_referral AS
SELECT
    p.referral_id,
    p.opportunity_id,
    p.referring_organisation_id                                                         AS organisation_id,
    p.participant_ref,
    date(p.received_at, c.tz_mod)                                                       AS received_date,
    p.funding_type,
    p.service_type_code,
    COALESCE(NULLIF(p.region_code, 'UNKNOWN'), org.region_code, 'UNKNOWN')             AS region_code,
    p.suburb,
    p.requested_weekly_hours,
    p.expected_hourly_rate,
    CASE WHEN p.requested_weekly_hours IS NOT NULL AND p.expected_hourly_rate IS NOT NULL
         THEN ROUND(p.requested_weekly_hours * p.expected_hourly_rate, 2) END           AS est_weekly_value,
    CASE WHEN p.requested_weekly_hours IS NOT NULL AND p.expected_hourly_rate IS NOT NULL
         THEN ROUND(p.requested_weekly_hours * p.expected_hourly_rate * cfg.weeks_per_month, 2) END AS est_monthly_value,
    CASE WHEN p.requested_weekly_hours IS NOT NULL AND p.expected_hourly_rate IS NOT NULL THEN 'KNOWN'
         ELSE 'UNKNOWN' END                                                             AS value_status,
    p.worker_gender_requirement,
    p.status,
    CASE WHEN p.status IN ('NEW', 'ASSESSING', 'ACCEPTED', 'WORKER_MATCHING', 'TRIAL_SCHEDULED') THEN 1 ELSE 0 END AS is_open,
    CASE WHEN p.status IN ('NEW', 'ASSESSING', 'ACCEPTED', 'WORKER_MATCHING') THEN 1 ELSE 0 END AS needs_action,
    datetime(p.response_due_at, c.tz_mod)                                               AS response_due_local,
    CASE WHEN p.status = 'NEW' AND p.response_due_at < c.now_utc THEN 1 ELSE 0 END      AS is_response_overdue,
    CASE WHEN p.funding_type = 'NDIS_AGENCY_MANAGED' AND cfg.registered_ndis_provider = 0 THEN 0 ELSE 1 END
                                                                                        AS is_billable_funding,
    p.next_action,
    p.next_action_due,
    p.decline_reason
FROM participant_referrals p
LEFT JOIN organisations org ON org.organisation_id = p.referring_organisation_id
CROSS JOIN v_ctx c
CROSS JOIN v_cfg cfg;

CREATE VIEW pbi_fact_trial_shift AS
SELECT
    t.trial_shift_id,
    t.referral_id,
    t.opportunity_id,
    COALESCE(t.organisation_id, p.referring_organisation_id)                           AS organisation_id,
    COALESCE(NULLIF(p.region_code, 'UNKNOWN'), org.region_code, 'UNKNOWN')             AS region_code,
    COALESCE(t.participant_ref, p.participant_ref)                                      AS participant_ref,
    t.worker_id,
    date(t.scheduled_start, c.tz_mod)                                                   AS trial_date,
    t.hours,
    t.status,
    t.outcome,
    CASE WHEN t.outcome = 'CONVERTED' THEN 1 ELSE 0 END                                 AS is_converted,
    CASE WHEN t.status IN ('SCHEDULED', 'COMPLETED') THEN 1 ELSE 0 END                  AS is_scheduled_or_done
FROM trial_shifts t
LEFT JOIN participant_referrals p ON p.referral_id = t.referral_id
LEFT JOIN organisations org ON org.organisation_id = COALESCE(t.organisation_id, p.referring_organisation_id)
CROSS JOIN v_ctx c;

CREATE VIEW pbi_fact_roster AS
SELECT
    ro.roster_id,
    ro.roster_ref,
    ro.participant_id,
    pa.participant_ref,
    rv.attributed_organisation_id                                                       AS organisation_id,
    rv.attributed_opportunity_id                                                        AS opportunity_id,
    op.source_code,
    op.opportunity_type_code,
    ro.region_code,
    COALESCE(ro.service_type_code, 'OTHER')                                             AS service_type_code,
    ro.primary_worker_id                                                                AS worker_id,
    ro.status,
    ro.revenue_model,
    ro.weekly_hours,
    ro.hourly_rate,
    ro.rate_basis,
    rv.weekly_value,
    rv.monthly_value,
    CASE WHEN rv.weekly_value IS NULL THEN 'UNKNOWN' ELSE 'KNOWN' END                   AS value_status,
    rv.counts_to_mrr,
    CASE WHEN rv.counts_to_mrr = 1 THEN rv.monthly_value ELSE 0 END                     AS mrr_contribution,
    CASE WHEN rv.counts_to_projected_mrr = 1 THEN rv.monthly_value ELSE 0 END           AS projected_mrr_contribution,
    rv.active_rate_not_approved,
    CASE WHEN ro.status = 'ACTIVE' THEN COALESCE(ro.weekly_hours, 0) ELSE 0 END         AS active_weekly_hours,
    -- Average billable rate uses the same approved-rate basis as MRR (quoted/unknown rates excluded).
    CASE WHEN rv.counts_to_mrr = 1 THEN ro.weekly_hours ELSE 0 END                      AS active_hours_with_rate,
    CASE WHEN rv.counts_to_mrr = 1 THEN ro.weekly_hours * ro.hourly_rate ELSE 0 END     AS active_weekly_value_with_rate,
    CASE WHEN ro.status = 'ACTIVE' THEN 1 ELSE 0 END                                    AS is_active,
    CASE WHEN ro.status = 'SCHEDULED' THEN 1 ELSE 0 END                                 AS is_scheduled,
    CASE WHEN ro.status IN ('ACTIVE', 'SCHEDULED') AND ro.primary_worker_id IS NULL THEN 1 ELSE 0 END AS is_unfilled,
    ro.includes_weekday,
    ro.includes_weekend,
    ro.includes_evening,
    ro.includes_overnight,
    ro.start_date,
    ro.end_date,
    COALESCE(oa.is_contacted, 0)                                                        AS attributed_org_contacted,
    CASE WHEN ro.status = 'ACTIVE' AND COALESCE(oa.is_contacted, 0) = 1 THEN COALESCE(ro.weekly_hours, 0) ELSE 0 END
                                                                                        AS active_hours_from_contacted_orgs
FROM rosters ro
JOIN v_roster_value rv ON rv.roster_id = ro.roster_id
LEFT JOIN participants pa ON pa.participant_id = ro.participant_id
LEFT JOIN opportunities op ON op.opportunity_id = rv.attributed_opportunity_id
LEFT JOIN v_org_activity oa ON oa.organisation_id = rv.attributed_organisation_id;

CREATE VIEW pbi_fact_revenue AS
SELECT
    r.revenue_id,
    r.invoice_number,
    r.invoice_date,
    r.service_date,
    date(r.service_date, 'start of month')                                              AS service_month,
    r.paid_date,
    r.attributed_organisation_id                                                        AS organisation_id,
    r.participant_id,
    r.roster_id,
    r.attributed_opportunity_id                                                         AS opportunity_id,
    COALESCE(op.source_code, 'OTHER')                                                   AS source_code,
    COALESCE(op.opportunity_type_code, 'OTHER')                                         AS opportunity_type_code,
    r.attributed_region_code                                                            AS region_code,
    r.attributed_service_type_code                                                      AS service_type_code,
    r.hours,
    r.hourly_rate,
    r.amount_ex_gst,
    r.recognised_amount,
    CASE WHEN r.status IN ('VOID', 'WRITTEN_OFF') THEN 0 ELSE r.amount_paid END         AS amount_received,
    r.status,
    r.payer_type,
    r.source_system,
    COALESCE(oa.is_contacted, 0)                                                        AS attributed_org_contacted,
    CASE WHEN COALESCE(oa.is_contacted, 0) = 1 THEN r.recognised_amount ELSE 0 END      AS revenue_from_contacted_orgs
FROM v_revenue_attributed r
LEFT JOIN opportunities op ON op.opportunity_id = r.attributed_opportunity_id
LEFT JOIN v_org_activity oa ON oa.organisation_id = r.attributed_organisation_id;

CREATE VIEW pbi_fact_partnership AS
SELECT
    p.partnership_id,
    p.organisation_id,
    org.region_code,
    p.partnership_type,
    p.stage_code                                                                        AS partnership_stage_code,
    ps.stage_name                                                                       AS partnership_stage_name,
    ps.stage_order                                                                      AS partnership_stage_order,
    p.status,
    p.onboarding_status,
    (SELECT a.status FROM associate_provider_applications a WHERE a.partnership_id = p.partnership_id
      ORDER BY a.application_id DESC LIMIT 1)                                           AS application_status,
    p.insurance_requirements,
    p.labour_hire_licence_required,
    p.ndis_registration_required,
    (SELECT COUNT(*) FROM partnership_documents d WHERE d.partnership_id = p.partnership_id
       AND d.status <> 'NOT_REQUIRED')                                                  AS documents_required,
    (SELECT COUNT(*) FROM partnership_documents d WHERE d.partnership_id = p.partnership_id
       AND d.status = 'OUTSTANDING')                                                    AS documents_outstanding,
    (SELECT group_concat(cd.doc_name, '; ') FROM partnership_documents d
       JOIN compliance_documents cd ON cd.doc_code = d.doc_code
      WHERE d.partnership_id = p.partnership_id AND d.status = 'OUTSTANDING')          AS documents_outstanding_list,
    p.documents_required_note,
    date(NULLIF(MAX(COALESCE(p.last_communication_at, ''), COALESCE(oa.last_activity_at, '')), ''), c.tz_mod)
                                                                                        AS last_communication_date,
    p.next_action,
    p.next_action_due,
    p.approval_date,
    p.first_work_received_date,
    p.agreed_hourly_rate,
    p.priority_tier,
    COALESCE((SELECT SUM(r.hours) FROM v_revenue_attributed r
               WHERE r.attributed_organisation_id = p.organisation_id AND r.recognised_amount > 0), 0) AS hours_received,
    COALESCE((SELECT SUM(rv.weekly_hours) FROM v_roster_value rv
               WHERE rv.attributed_organisation_id = p.organisation_id AND rv.status = 'ACTIVE'), 0) AS active_weekly_hours,
    COALESCE((SELECT SUM(r.recognised_amount) FROM v_revenue_attributed r
               WHERE r.attributed_organisation_id = p.organisation_id), 0)             AS revenue_generated,
    CASE WHEN p.status = 'ACTIVE' THEN 1 ELSE 0 END                                     AS is_active
FROM partnerships p
JOIN organisations org ON org.organisation_id = p.organisation_id
JOIN ref_partnership_stage ps ON ps.stage_code = p.stage_code
LEFT JOIN v_org_activity oa ON oa.organisation_id = p.organisation_id
CROSS JOIN v_ctx c;

CREATE VIEW pbi_fact_application AS
SELECT
    a.application_id,
    a.partnership_id,
    a.organisation_id,
    org.region_code,
    p.partnership_type,
    a.application_method,
    a.application_url,
    a.status,
    a.requires_ritik,
    CASE WHEN a.requires_ritik = 1 AND a.status IN ('NOT_STARTED', 'IN_PROGRESS', 'MORE_INFO_REQUESTED')
         THEN 1 ELSE 0 END                                                              AS needs_completion,
    a.priority_tier,
    date(a.submitted_at, c.tz_mod)                                                      AS submitted_date,
    date(a.decision_at, c.tz_mod)                                                       AS decision_date,
    a.next_action,
    a.next_action_due,
    a.notes
FROM associate_provider_applications a
JOIN organisations org ON org.organisation_id = a.organisation_id
LEFT JOIN partnerships p ON p.partnership_id = a.partnership_id
CROSS JOIN v_ctx c;

CREATE VIEW pbi_fact_compliance_document AS
SELECT
    d.doc_code,
    d.doc_name,
    d.is_held,
    d.expiry_date,
    CASE WHEN d.is_held = 1 AND (d.expiry_date IS NULL OR d.expiry_date >= c.today) THEN 1 ELSE 0 END AS is_current,
    CASE WHEN d.is_held = 0 THEN 'MISSING'
         WHEN d.expiry_date IS NOT NULL AND d.expiry_date < c.today THEN 'EXPIRED'
         WHEN d.expiry_date IS NOT NULL AND d.expiry_date < date(c.today, '+30 days') THEN 'EXPIRING'
         ELSE 'CURRENT' END                                                             AS status_label,
    d.sort_order,
    (SELECT COUNT(*) FROM partnership_documents pd WHERE pd.doc_code = d.doc_code
       AND pd.status = 'OUTSTANDING')                                                   AS partnerships_waiting
FROM compliance_documents d
CROSS JOIN v_ctx c;

CREATE VIEW pbi_fact_worker_availability AS
SELECT
    s.availability_id,
    s.worker_id,
    COALESCE(s.region_code, w.home_region_code)                                         AS region_code,
    s.day_of_week,
    CASE s.day_of_week WHEN 1 THEN 'Mon' WHEN 2 THEN 'Tue' WHEN 3 THEN 'Wed' WHEN 4 THEN 'Thu'
                       WHEN 5 THEN 'Fri' WHEN 6 THEN 'Sat' ELSE 'Sun' END              AS day_name,
    s.start_time,
    s.end_time,
    s.slot_hours,
    s.is_weekday,
    s.is_weekend,
    s.is_evening,
    s.is_overnight,
    s.is_confirmed,
    s.is_current,
    CASE WHEN v.is_verified_active = 1 AND s.is_confirmed = 1 AND s.is_current = 1 THEN 1 ELSE 0 END AS counts_as_capacity,
    CASE WHEN v.is_verified_active = 1 AND s.is_confirmed = 1 AND s.is_current = 1 THEN s.slot_hours ELSE 0 END
                                                                                        AS capacity_hours
FROM v_availability_slots s
JOIN workers w ON w.worker_id = s.worker_id
JOIN v_worker_verified v ON v.worker_id = s.worker_id;

CREATE VIEW pbi_fact_worker_capacity AS
SELECT
    w.worker_id,
    w.home_region_code                                                                  AS region_code,
    COALESCE(w.gender_for_matching, 'UNDISCLOSED')                                      AS gender_for_matching,
    w.status,
    cc.is_verified_active,
    NULLIF(cc.verification_gaps, '')                                                    AS verification_gaps,
    cc.capacity_weekly_hours,
    cc.allocated_weekly_hours,
    cc.available_weekly_hours,
    CASE WHEN cc.capacity_weekly_hours > 0
         THEN ROUND(cc.allocated_weekly_hours / cc.capacity_weekly_hours, 4) END        AS utilisation,
    CASE WHEN cc.is_verified_active = 1 AND cc.available_weekly_hours > 0 THEN 1 ELSE 0 END AS is_available,
    w.cap_personal_care,
    w.cap_domestic,
    w.cap_community_access,
    w.cap_complex_support
FROM workers w
JOIN v_worker_capacity_calc cc ON cc.worker_id = w.worker_id;

CREATE VIEW pbi_fact_worker_requirement AS
SELECT
    q.requirement_id,
    q.organisation_id,
    q.referral_id,
    q.roster_id,
    q.region_code,
    q.service_type_code,
    q.required_capability,
    COALESCE(q.gender_requirement, 'ANY')                                               AS gender_requirement,
    q.weekly_hours,
    q.needs_weekday,
    q.needs_weekend,
    q.needs_evening,
    q.needs_overnight,
    q.needed_by,
    q.status,
    CASE WHEN q.status = 'OPEN' THEN 1 ELSE 0 END                                       AS is_open
FROM worker_requirements q;

CREATE VIEW pbi_fact_agent_health AS
SELECT
    h.agent_code,
    h.agent_name,
    h.schedule_description,
    h.expected_interval_hours,
    datetime(h.last_successful_run, c.tz_mod)                                           AS last_successful_run,
    datetime(h.last_attempted_run, c.tz_mod)                                            AS last_attempted_run,
    h.last_run_status,
    h.last_runtime_seconds,
    h.records_processed,
    h.records_created,
    h.errors_count,
    h.warnings_count,
    h.last_error,
    datetime(h.last_error_at, c.tz_mod)                                                 AS last_error_at,
    datetime(h.next_scheduled_run, c.tz_mod)                                            AS next_scheduled_run,
    h.hours_since_success,
    h.rag_status,
    CASE h.rag_status WHEN 'RED' THEN '🔴 RED' WHEN 'AMBER' THEN '🟠 AMBER' WHEN 'GREEN' THEN '🟢 GREEN'
         ELSE '⚪ ' || h.rag_status END                                                AS rag_label,
    CASE h.rag_status WHEN 'RED' THEN 1 WHEN 'AMBER' THEN 2 WHEN 'GREEN' THEN 3 ELSE 4 END AS rag_sort,
    h.rag_reason
FROM v_agent_health h
CROSS JOIN v_ctx c;

CREATE VIEW pbi_fact_agent_run AS
SELECT
    r.run_id,
    r.agent_code,
    date(r.started_at, c.tz_mod)                                                        AS run_date,
    datetime(r.started_at, c.tz_mod)                                                    AS started_local,
    datetime(r.finished_at, c.tz_mod)                                                   AS finished_local,
    r.status,
    r.trigger_type,
    CASE WHEN r.finished_at IS NOT NULL
         THEN ROUND((julianday(r.finished_at) - julianday(r.started_at)) * 86400) END  AS runtime_seconds,
    r.records_processed,
    r.records_created,
    r.records_updated,
    r.records_skipped,
    r.errors_count,
    r.warnings_count,
    r.last_error
FROM agent_runs r
CROSS JOIN v_ctx c
WHERE r.started_at >= date(c.today, '-90 days');

CREATE VIEW pbi_fact_alert AS
SELECT
    a.alert_id,
    a.source_type,
    a.agent_code,
    a.severity,
    CASE a.severity WHEN 'CRITICAL' THEN 1 WHEN 'ERROR' THEN 2 WHEN 'WARNING' THEN 3 ELSE 4 END AS severity_sort,
    a.category,
    a.entity_type,
    a.entity_id,
    a.message,
    datetime(a.first_seen_at, c.tz_mod)                                                 AS first_seen_local,
    datetime(a.last_seen_at, c.tz_mod)                                                  AS last_seen_local,
    a.occurrences,
    a.status
FROM alerts a
CROSS JOIN v_ctx c
WHERE a.status <> 'RESOLVED' OR a.resolved_at >= date(c.today, '-14 days');

CREATE VIEW pbi_fact_data_quality AS
SELECT
    r.result_id,
    r.check_code,
    r.severity,
    CASE r.severity WHEN 'ERROR' THEN 1 WHEN 'WARNING' THEN 2 ELSE 3 END                AS severity_sort,
    r.entity_type,
    r.entity_id,
    r.organisation_id,
    r.detail,
    datetime(q.run_at, c.tz_mod)                                                        AS checked_at_local
FROM data_quality_results r
JOIN data_quality_runs q ON q.check_run_id = r.check_run_id
CROSS JOIN v_ctx c
WHERE r.check_run_id = (SELECT MAX(check_run_id) FROM data_quality_runs);

CREATE VIEW pbi_fact_daily_snapshot AS
SELECT * FROM daily_metrics_snapshot;

CREATE VIEW pbi_targets AS
SELECT
    t.target_code,
    t.target_value,
    t.unit,
    t.description,
    t.effective_from,
    (SELECT date(MIN(n.effective_from), '-1 day') FROM business_targets n
      WHERE n.target_code = t.target_code AND n.effective_from > t.effective_from) AS effective_to
FROM business_targets t;
