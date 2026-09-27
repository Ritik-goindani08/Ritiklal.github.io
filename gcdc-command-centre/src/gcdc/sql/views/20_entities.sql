-- =============================================================================
-- 20: derived entity views. All business rules for stages, values, capacity
-- and health live here so Python, the daily brief and Power BI agree.
-- =============================================================================

-- Primary opportunity of each organisation: events that arrive without an
-- opportunity_id are attributed here (see gcdc.reconcile).
CREATE VIEW v_org_primary_opportunity AS
SELECT
    org.organisation_id,
    COALESCE(
        (SELECT o.opportunity_id FROM opportunities o
          WHERE o.organisation_id = org.organisation_id AND o.status = 'OPEN'
          ORDER BY o.opportunity_id LIMIT 1),
        (SELECT o.opportunity_id FROM opportunities o
          WHERE o.organisation_id = org.organisation_id
          ORDER BY o.opportunity_id LIMIT 1)) AS primary_opportunity_id
FROM organisations org
WHERE org.merged_into_id IS NULL;

-- ---------------------------------------------------------------------------
-- Opportunity stage evidence. The effective stage is the furthest of
--   (a) the stage recorded on the opportunity, and
--   (b) the furthest stage proven by linked records.
-- Stages never move backwards automatically.
-- ---------------------------------------------------------------------------
CREATE VIEW v_opportunity_evidence AS
SELECT
    o.opportunity_id,
    s.stage_order AS recorded_stage_order,
    MAX(
        CASE WHEN o.is_verified = 1 THEN 2 ELSE 1 END,
        CASE WHEN EXISTS (SELECT 1 FROM outreach x WHERE x.opportunity_id = o.opportunity_id
                          AND x.status = 'SENT') THEN 3 ELSE 0 END,
        CASE WHEN EXISTS (SELECT 1 FROM replies r WHERE r.opportunity_id = o.opportunity_id
                          AND r.classification NOT IN ('OUT_OF_OFFICE', 'AUTO_REPLY')) THEN 4 ELSE 0 END,
        CASE WHEN EXISTS (SELECT 1 FROM replies r WHERE r.opportunity_id = o.opportunity_id
                          AND r.classification IN ('POSITIVE', 'REFERRAL')) THEN 5 ELSE 0 END,
        CASE WHEN EXISTS (SELECT 1 FROM meetings m WHERE m.opportunity_id = o.opportunity_id
                          AND m.status IN ('BOOKED', 'COMPLETED', 'RESCHEDULED')) THEN 6 ELSE 0 END,
        CASE WHEN EXISTS (SELECT 1 FROM participant_referrals p WHERE p.opportunity_id = o.opportunity_id)
             THEN 7 ELSE 0 END,
        CASE WHEN EXISTS (SELECT 1 FROM trial_shifts t WHERE t.opportunity_id = o.opportunity_id
                          AND t.status IN ('SCHEDULED', 'COMPLETED')) THEN 8 ELSE 0 END,
        CASE WHEN EXISTS (SELECT 1 FROM rosters ro WHERE ro.opportunity_id = o.opportunity_id
                          AND ro.status IN ('SCHEDULED', 'ACTIVE', 'PAUSED', 'ENDED')) THEN 9 ELSE 0 END
    ) AS evidence_stage_order,
    NULLIF(MAX(
        COALESCE(o.discovered_at, ''),
        COALESCE(o.stage_changed_at, ''),
        COALESCE((SELECT MAX(x.sent_at) FROM outreach x WHERE x.opportunity_id = o.opportunity_id), ''),
        COALESCE((SELECT MAX(r.received_at) FROM replies r WHERE r.opportunity_id = o.opportunity_id), ''),
        COALESCE((SELECT MAX(m.scheduled_start) FROM meetings m, v_ctx c
                   WHERE m.opportunity_id = o.opportunity_id AND m.scheduled_start <= c.now_utc), ''),
        COALESCE((SELECT MAX(p.received_at) FROM participant_referrals p WHERE p.opportunity_id = o.opportunity_id), ''),
        COALESCE((SELECT MAX(t.scheduled_start) FROM trial_shifts t, v_ctx c
                   WHERE t.opportunity_id = o.opportunity_id AND t.scheduled_start <= c.now_utc), '')
    ), '') AS last_activity_at
FROM opportunities o
JOIN ref_pipeline_stage s ON s.stage_code = o.stage_code;

-- Opportunity value & horizon. Values are NULL (UNKNOWN) unless both hours and
-- rate are known — from the opportunity itself or its open referral.
CREATE VIEW v_opportunity_status AS
SELECT
    o.opportunity_id,
    o.organisation_id,
    ev.recorded_stage_order,
    ev.evidence_stage_order,
    MAX(ev.recorded_stage_order, ev.evidence_stage_order)                AS stage_order_reached,
    (SELECT s2.stage_code FROM ref_pipeline_stage s2
      WHERE s2.stage_order = MAX(ev.recorded_stage_order, ev.evidence_stage_order))   AS effective_stage_code,
    ev.last_activity_at,
    COALESCE(o.expected_weekly_hours, ref.requested_weekly_hours)        AS expected_weekly_hours,
    COALESCE(o.expected_hourly_rate, ref.expected_hourly_rate)           AS expected_hourly_rate,
    COALESCE(o.rate_basis, ref.rate_basis)                               AS rate_basis,
    CASE WHEN o.expected_weekly_hours IS NOT NULL OR o.expected_hourly_rate IS NOT NULL THEN 'OPPORTUNITY'
         WHEN ref.referral_id IS NOT NULL THEN 'REFERRAL' END              AS value_basis,
    ref.participant_ref                                                   AS referral_participant_ref
FROM opportunities o
JOIN v_opportunity_evidence ev ON ev.opportunity_id = o.opportunity_id
LEFT JOIN participant_referrals ref ON ref.referral_id = (
    SELECT p.referral_id FROM participant_referrals p
     WHERE p.opportunity_id = o.opportunity_id
       AND p.status NOT IN ('DECLINED', 'LOST', 'WITHDRAWN')
     ORDER BY p.received_at DESC LIMIT 1);

CREATE VIEW v_opportunity_value AS
SELECT
    st.*,
    CASE WHEN st.expected_weekly_hours IS NOT NULL AND st.expected_hourly_rate IS NOT NULL
         THEN ROUND(st.expected_weekly_hours * st.expected_hourly_rate, 2) END              AS est_weekly_value,
    CASE WHEN st.expected_weekly_hours IS NOT NULL AND st.expected_hourly_rate IS NOT NULL
         THEN ROUND(st.expected_weekly_hours * st.expected_hourly_rate * cfg.weeks_per_month, 2) END AS est_monthly_value,
    CASE WHEN st.expected_weekly_hours IS NOT NULL AND st.expected_hourly_rate IS NOT NULL THEN 'KNOWN'
         WHEN st.expected_weekly_hours IS NULL AND st.expected_hourly_rate IS NULL THEN 'UNKNOWN_HOURS_AND_RATE'
         WHEN st.expected_weekly_hours IS NULL THEN 'UNKNOWN_HOURS'
         ELSE 'UNKNOWN_RATE' END                                                           AS value_status,
    CASE WHEN cfg.use_stage_probabilities = 1 THEN s.default_probability END              AS stage_probability,
    CASE WHEN o.status IN ('WON') OR st.stage_order_reached >= 9 THEN 'CONVERTED'
         WHEN o.status <> 'OPEN' THEN 'CLOSED'
         WHEN st.stage_order_reached >= nt.near_term_min_order THEN 'NEAR_TERM'
         ELSE 'LONGER_TERM' END                                                             AS pipeline_horizon,
    CASE WHEN o.status = 'OPEN' AND st.stage_order_reached < 9 THEN 1 ELSE 0 END           AS is_active
FROM v_opportunity_status st
JOIN opportunities o ON o.opportunity_id = st.opportunity_id
JOIN ref_pipeline_stage s ON s.stage_code = st.effective_stage_code
CROSS JOIN v_cfg cfg
CROSS JOIN v_near_term_stage nt;

-- ---------------------------------------------------------------------------
-- Organisation-level activity (drives conversion rates per 100 contacted)
-- ---------------------------------------------------------------------------
CREATE VIEW v_org_activity AS
SELECT
    org.organisation_id,
    (SELECT MIN(x.sent_at) FROM outreach x WHERE x.organisation_id = org.organisation_id AND x.status = 'SENT') AS first_contacted_at,
    (SELECT MAX(x.sent_at) FROM outreach x WHERE x.organisation_id = org.organisation_id AND x.status = 'SENT') AS last_contacted_at,
    (SELECT COUNT(*) FROM outreach x WHERE x.organisation_id = org.organisation_id AND x.status = 'SENT')       AS touches_sent,
    EXISTS (SELECT 1 FROM outreach x WHERE x.organisation_id = org.organisation_id AND x.status = 'SENT')       AS is_contacted,
    EXISTS (SELECT 1 FROM replies r WHERE r.organisation_id = org.organisation_id
            AND r.classification NOT IN ('OUT_OF_OFFICE', 'AUTO_REPLY'))                                         AS has_replied,
    EXISTS (SELECT 1 FROM replies r WHERE r.organisation_id = org.organisation_id
            AND r.classification IN ('POSITIVE', 'REFERRAL'))                                                    AS has_positive,
    EXISTS (SELECT 1 FROM meetings m WHERE m.organisation_id = org.organisation_id
            AND m.status IN ('BOOKED', 'COMPLETED', 'RESCHEDULED'))                                              AS has_meeting,
    EXISTS (SELECT 1 FROM participant_referrals p WHERE p.referring_organisation_id = org.organisation_id)       AS has_referral,
    EXISTS (SELECT 1 FROM trial_shifts t WHERE t.organisation_id = org.organisation_id
            AND t.status IN ('SCHEDULED', 'COMPLETED'))                                                           AS has_trial,
    EXISTS (SELECT 1 FROM rosters ro WHERE ro.organisation_id = org.organisation_id
            AND ro.status IN ('SCHEDULED', 'ACTIVE', 'PAUSED', 'ENDED'))                                         AS has_roster,
    NULLIF(MAX(
        COALESCE((SELECT MAX(x.sent_at) FROM outreach x WHERE x.organisation_id = org.organisation_id), ''),
        COALESCE((SELECT MAX(r.received_at) FROM replies r WHERE r.organisation_id = org.organisation_id), ''),
        COALESCE((SELECT MAX(m.scheduled_start) FROM meetings m, v_ctx c
                   WHERE m.organisation_id = org.organisation_id AND m.scheduled_start <= c.now_utc), ''),
        COALESCE((SELECT MAX(p.received_at) FROM participant_referrals p
                   WHERE p.referring_organisation_id = org.organisation_id), '')
    ), '') AS last_activity_at
FROM organisations org
WHERE org.merged_into_id IS NULL;

-- ---------------------------------------------------------------------------
-- Rosters -> recurring revenue. MRR counts ACTIVE rosters whose rate basis is
-- in revenue.mrr_rate_bases (default APPROVED only). Unknown rate = no dollars.
-- ---------------------------------------------------------------------------
CREATE VIEW v_roster_value AS
SELECT
    ro.roster_id,
    ro.status,
    ro.weekly_hours,
    ro.hourly_rate,
    ro.rate_basis,
    CASE WHEN ro.weekly_hours IS NOT NULL AND ro.hourly_rate IS NOT NULL
         THEN ROUND(ro.weekly_hours * ro.hourly_rate, 2) END                              AS weekly_value,
    CASE WHEN ro.weekly_hours IS NOT NULL AND ro.hourly_rate IS NOT NULL
         THEN ROUND(ro.weekly_hours * ro.hourly_rate * cfg.weeks_per_month, 2) END        AS monthly_value,
    CASE WHEN ro.status = 'ACTIVE' AND ro.weekly_hours IS NOT NULL AND ro.hourly_rate IS NOT NULL
          AND ro.rate_basis IN (SELECT value FROM json_each(cfg.mrr_rate_bases))
         THEN 1 ELSE 0 END                                                                  AS counts_to_mrr,
    CASE WHEN ro.status = 'ACTIVE' AND (ro.hourly_rate IS NULL
          OR ro.rate_basis IS NULL OR ro.rate_basis NOT IN (SELECT value FROM json_each(cfg.mrr_rate_bases)))
         THEN 1 ELSE 0 END                                                                  AS active_rate_not_approved,
    CASE WHEN ro.status IN ('ACTIVE', 'SCHEDULED') AND ro.weekly_hours IS NOT NULL AND ro.hourly_rate IS NOT NULL
          AND ro.rate_basis IN (SELECT value FROM json_each(cfg.mrr_rate_bases))
         THEN 1 ELSE 0 END                                                                  AS counts_to_projected_mrr,
    COALESCE(ro.organisation_id, p.referring_organisation_id)                              AS attributed_organisation_id,
    COALESCE(ro.opportunity_id, ref.opportunity_id)                                        AS attributed_opportunity_id
FROM rosters ro
LEFT JOIN participants p ON p.participant_id = ro.participant_id
LEFT JOIN participant_referrals ref ON ref.referral_id = COALESCE(ro.referral_id, p.referral_id)
CROSS JOIN v_cfg cfg;

-- Revenue actuals with attribution to organisation / opportunity / source / region.
CREATE VIEW v_revenue_attributed AS
SELECT
    rt.*,
    COALESCE(rt.organisation_id, ro.organisation_id, p.referring_organisation_id)          AS attributed_organisation_id,
    COALESCE(rt.opportunity_id, ro.opportunity_id, ref.opportunity_id)                      AS attributed_opportunity_id,
    COALESCE(rt.region_code, ro.region_code, p.region_code, org.region_code, 'UNKNOWN')     AS attributed_region_code,
    COALESCE(rt.service_type_code, ro.service_type_code, 'OTHER')                           AS attributed_service_type_code,
    COALESCE(rt.service_period_start, rt.invoice_date)                                      AS service_date,
    CASE WHEN rt.status IN ('VOID', 'WRITTEN_OFF') THEN 0 ELSE rt.amount_ex_gst END         AS recognised_amount
FROM revenue_transactions rt
LEFT JOIN rosters ro ON ro.roster_id = rt.roster_id
LEFT JOIN participants p ON p.participant_id = COALESCE(rt.participant_id, ro.participant_id)
LEFT JOIN participant_referrals ref ON ref.referral_id = COALESCE(ro.referral_id, p.referral_id)
LEFT JOIN organisations org ON org.organisation_id = COALESCE(rt.organisation_id, ro.organisation_id, p.referring_organisation_id);

-- ---------------------------------------------------------------------------
-- Workers: only verified, current workers count as capacity.
-- ---------------------------------------------------------------------------
CREATE VIEW v_worker_verified AS
SELECT
    w.worker_id,
    w.status,
    CASE WHEN w.status = 'ACTIVE'
          AND (cfg.req_ndis_screening = 0 OR (w.ndis_screening_verified = 1
               AND (w.ndis_screening_expiry IS NULL OR w.ndis_screening_expiry >= c.today)))
          AND (cfg.req_first_aid = 0 OR w.first_aid_expiry >= c.today)
          AND (cfg.req_cpr = 0 OR w.cpr_expiry >= c.today)
          AND (cfg.req_blue_card = 0 OR (w.blue_card_verified = 1
               AND (w.blue_card_expiry IS NULL OR w.blue_card_expiry >= c.today)))
          AND (cfg.req_police_check = 0 OR w.police_check_verified = 1)
         THEN 1 ELSE 0 END AS is_verified_active,
    TRIM(
        CASE WHEN cfg.req_ndis_screening = 1 AND (w.ndis_screening_verified = 0
                  OR w.ndis_screening_expiry < c.today) THEN 'NDIS screening; ' ELSE '' END ||
        CASE WHEN cfg.req_first_aid = 1 AND (w.first_aid_expiry IS NULL OR w.first_aid_expiry < c.today)
             THEN 'First aid; ' ELSE '' END ||
        CASE WHEN cfg.req_cpr = 1 AND (w.cpr_expiry IS NULL OR w.cpr_expiry < c.today) THEN 'CPR; ' ELSE '' END ||
        CASE WHEN cfg.req_blue_card = 1 AND (w.blue_card_verified = 0 OR w.blue_card_expiry < c.today)
             THEN 'Blue card; ' ELSE '' END ||
        CASE WHEN cfg.req_police_check = 1 AND w.police_check_verified = 0 THEN 'Police check; ' ELSE '' END
    ) AS verification_gaps
FROM workers w
CROSS JOIN v_cfg cfg
CROSS JOIN v_ctx c;

-- Availability slots with hours and time bands. Slots may cross midnight.
CREATE VIEW v_availability_slots AS
SELECT
    a.*,
    x.start_min,
    x.end_min,
    ROUND((CASE WHEN x.end_min > x.start_min THEN x.end_min - x.start_min
                ELSE x.end_min + 1440 - x.start_min END) / 60.0, 2)                        AS slot_hours,
    CASE WHEN a.day_of_week BETWEEN 1 AND 5 THEN 1 ELSE 0 END                               AS is_weekday,
    CASE WHEN a.day_of_week IN (6, 7) THEN 1 ELSE 0 END                                     AS is_weekend,
    CASE WHEN x.start_min < 1320 AND (x.end_min > 1080 OR x.end_min <= x.start_min)
         THEN 1 ELSE 0 END                                                                   AS is_evening,
    CASE WHEN x.end_min <= x.start_min OR x.start_min >= 1320 OR x.start_min < 360
         THEN 1 ELSE 0 END                                                                   AS is_overnight,
    CASE WHEN (a.effective_from IS NULL OR a.effective_from <= c.today)
          AND (a.effective_to IS NULL OR a.effective_to >= c.today) THEN 1 ELSE 0 END      AS is_current
FROM worker_availability a
CROSS JOIN v_ctx c
JOIN (
    SELECT availability_id,
           CAST(substr(start_time, 1, 2) AS INTEGER) * 60 + CAST(substr(start_time, 4, 2) AS INTEGER) AS start_min,
           CAST(substr(end_time, 1, 2) AS INTEGER) * 60 + CAST(substr(end_time, 4, 2) AS INTEGER)     AS end_min
    FROM worker_availability
) x ON x.availability_id = a.availability_id;

-- Per-worker capacity: confirmed, current availability of verified workers only.
CREATE VIEW v_worker_capacity AS
SELECT
    w.worker_id,
    v.is_verified_active,
    v.verification_gaps,
    COALESCE((SELECT SUM(s.slot_hours) FROM v_availability_slots s
               WHERE s.worker_id = w.worker_id AND s.is_confirmed = 1 AND s.is_current = 1), 0)  AS slot_hours_raw,
    w.max_weekly_hours,
    COALESCE((SELECT SUM(ro.weekly_hours) FROM rosters ro
               WHERE ro.primary_worker_id = w.worker_id AND ro.status = 'ACTIVE'), 0)             AS allocated_weekly_hours
FROM workers w
JOIN v_worker_verified v ON v.worker_id = w.worker_id;

CREATE VIEW v_worker_capacity_calc AS
SELECT
    wc.*,
    CASE WHEN wc.is_verified_active = 1
         THEN CASE WHEN wc.max_weekly_hours IS NOT NULL AND wc.max_weekly_hours < wc.slot_hours_raw
                   THEN wc.max_weekly_hours ELSE wc.slot_hours_raw END
         ELSE 0 END                                                                          AS capacity_weekly_hours,
    CASE WHEN wc.is_verified_active = 1
         THEN MAX(0, (CASE WHEN wc.max_weekly_hours IS NOT NULL AND wc.max_weekly_hours < wc.slot_hours_raw
                           THEN wc.max_weekly_hours ELSE wc.slot_hours_raw END) - wc.allocated_weekly_hours)
         ELSE 0 END                                                                          AS available_weekly_hours
FROM v_worker_capacity wc;

-- ---------------------------------------------------------------------------
-- Agent health: GREEN operating normally / AMBER warning or late / RED failed,
-- stuck, never reported, or far overdue.
-- ---------------------------------------------------------------------------
CREATE VIEW v_agent_last_runs AS
SELECT
    a.agent_code,
    (SELECT r.run_id FROM agent_runs r WHERE r.agent_code = a.agent_code
      ORDER BY r.started_at DESC, r.run_id DESC LIMIT 1)                                   AS last_run_id,
    (SELECT r.run_id FROM agent_runs r WHERE r.agent_code = a.agent_code AND r.status IN ('SUCCESS', 'WARNING')
      ORDER BY r.started_at DESC, r.run_id DESC LIMIT 1)                                   AS last_success_run_id,
    (SELECT r.run_id FROM agent_runs r WHERE r.agent_code = a.agent_code AND r.last_error IS NOT NULL
      ORDER BY r.started_at DESC, r.run_id DESC LIMIT 1)                                   AS last_error_run_id
FROM agents a;

CREATE VIEW v_agent_health AS
SELECT
    a.agent_code,
    a.agent_name,
    a.schedule_description,
    a.expected_interval_hours,
    a.business_days_only,
    a.is_active,
    lr.started_at                                                                           AS last_attempted_run,
    lr.status                                                                               AS last_run_status,
    ls.finished_at                                                                          AS last_successful_run,
    CASE WHEN lr.finished_at IS NOT NULL
         THEN ROUND((julianday(lr.finished_at) - julianday(lr.started_at)) * 86400) END    AS last_runtime_seconds,
    lr.records_processed,
    lr.records_created,
    lr.records_updated,
    lr.errors_count,
    lr.warnings_count,
    le.last_error,
    le.started_at                                                                           AS last_error_at,
    lr.next_scheduled_run,
    CASE WHEN ls.finished_at IS NOT NULL
         THEN ROUND((julianday(c.now_utc) - julianday(ls.finished_at)) * 24, 1) END        AS hours_since_success,
    a.expected_interval_hours
      + CASE WHEN a.business_days_only = 1 AND strftime('%w', c.today) IN ('0', '1', '6') THEN 48 ELSE 0 END
                                                                                            AS effective_interval_hours,
    CASE
        WHEN a.is_active = 0 THEN 'INACTIVE'
        WHEN lr.run_id IS NULL THEN 'RED'
        WHEN lr.status IN ('FAILED', 'ABORTED') THEN 'RED'
        WHEN lr.status = 'RUNNING' AND (julianday(c.now_utc) - julianday(lr.started_at)) * 24 > 6 THEN 'RED'
        WHEN ls.run_id IS NULL AND lr.status = 'RUNNING' THEN 'AMBER'
        WHEN ls.run_id IS NULL THEN 'RED'
        WHEN a.expected_interval_hours IS NOT NULL
         AND (julianday(c.now_utc) - julianday(ls.finished_at)) * 24 >
             (a.expected_interval_hours + CASE WHEN a.business_days_only = 1
                AND strftime('%w', c.today) IN ('0', '1', '6') THEN 48 ELSE 0 END) * cfg.red_multiplier THEN 'RED'
        WHEN lr.status = 'WARNING' THEN 'AMBER'
        WHEN a.expected_interval_hours IS NOT NULL
         AND (julianday(c.now_utc) - julianday(ls.finished_at)) * 24 >
             (a.expected_interval_hours + CASE WHEN a.business_days_only = 1
                AND strftime('%w', c.today) IN ('0', '1', '6') THEN 48 ELSE 0 END) * cfg.amber_multiplier THEN 'AMBER'
        ELSE 'GREEN'
    END AS rag_status,
    CASE
        WHEN a.is_active = 0 THEN 'Agent disabled in config'
        WHEN lr.run_id IS NULL THEN 'No runs recorded — agent is not reporting to the central database'
        WHEN lr.status IN ('FAILED', 'ABORTED') THEN 'Last run failed: ' || COALESCE(lr.last_error, 'no error text')
        WHEN lr.status = 'RUNNING' AND (julianday(c.now_utc) - julianday(lr.started_at)) * 24 > 6
             THEN 'Run started ' || lr.started_at || ' and never finished'
        WHEN ls.run_id IS NULL AND lr.status = 'RUNNING' THEN 'First run in progress'
        WHEN ls.run_id IS NULL THEN 'Has never completed successfully'
        WHEN lr.status = 'WARNING' THEN 'Last run finished with warnings'
        ELSE 'Last success ' || ls.finished_at
    END AS rag_reason
FROM agents a
JOIN v_agent_last_runs l ON l.agent_code = a.agent_code
LEFT JOIN agent_runs lr ON lr.run_id = l.last_run_id
LEFT JOIN agent_runs ls ON ls.run_id = l.last_success_run_id
LEFT JOIN agent_runs le ON le.run_id = l.last_error_run_id
CROSS JOIN v_ctx c
CROSS JOIN v_cfg cfg;
