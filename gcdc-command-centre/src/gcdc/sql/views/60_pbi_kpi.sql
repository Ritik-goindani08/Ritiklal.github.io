-- =============================================================================
-- 60: company-level KPIs (one row), funnel, and export metadata.
-- pbi_kpi_current is the single definition used by the Executive cards, the
-- daily snapshot and the Daily BI brief, so all three always agree.
-- =============================================================================

CREATE VIEW pbi_kpi_current AS
SELECT
    c.today                                                                              AS as_of_date,
    c.now_local                                                                          AS as_of_local,
    -- ---- revenue: actuals -------------------------------------------------------
    ROUND(COALESCE((SELECT SUM(mrr_contribution) FROM pbi_fact_roster), 0), 2)           AS mrr,
    (SELECT COUNT(*) FROM pbi_fact_roster WHERE is_active = 1 AND active_rate_not_approved = 1) AS mrr_rosters_rate_unknown,
    ROUND(COALESCE((SELECT SUM(projected_mrr_contribution) FROM pbi_fact_roster), 0), 2) AS projected_mrr,
    ROUND(COALESCE((SELECT SUM(active_weekly_hours) FROM pbi_fact_roster), 0), 2)        AS weekly_billable_hours,
    (SELECT ROUND(SUM(active_weekly_value_with_rate) / NULLIF(SUM(active_hours_with_rate), 0), 2)
       FROM pbi_fact_roster)                                                             AS avg_billable_rate,
    t.mrr_target                                                                         AS revenue_target,
    ROUND(MAX(0, COALESCE(t.mrr_target, 0) - COALESCE((SELECT SUM(mrr_contribution) FROM pbi_fact_roster), 0)), 2)
                                                                                         AS revenue_gap,
    CASE WHEN t.mrr_target > 0 THEN ROUND(COALESCE((SELECT SUM(mrr_contribution) FROM pbi_fact_roster), 0)
                                          / t.mrr_target, 4) END                         AS mrr_pct_of_target,
    t.weekly_hours_min                                                                   AS weekly_hours_target_min,
    t.weekly_hours_max                                                                   AS weekly_hours_target_max,
    ROUND(MAX(0, COALESCE(t.weekly_hours_min, 0) - COALESCE((SELECT SUM(active_weekly_hours) FROM pbi_fact_roster), 0)), 2)
                                                                                         AS weekly_hours_gap,
    ROUND(COALESCE((SELECT SUM(recognised_amount) FROM pbi_fact_revenue WHERE service_date >= c.month_start
                     AND service_date <= c.today), 0), 2)                                AS revenue_invoiced_mtd,
    ROUND(COALESCE((SELECT SUM(amount_received) FROM pbi_fact_revenue WHERE paid_date >= c.month_start
                     AND paid_date <= c.today), 0), 2)                                   AS revenue_received_mtd,
    ROUND(COALESCE((SELECT SUM(amount_received) FROM pbi_fact_revenue WHERE paid_date = c.today), 0), 2)
                                                                                         AS revenue_received_today,
    ROUND(COALESCE((SELECT SUM(amount_received) FROM pbi_fact_revenue WHERE paid_date >= c.prev_month_start
                     AND paid_date < c.month_start), 0), 2)                              AS revenue_received_prev_month,
    (SELECT COUNT(*) FROM revenue_transactions)                                          AS revenue_records,
    (SELECT COUNT(DISTINCT participant_id) FROM pbi_fact_roster WHERE is_active = 1 AND participant_id IS NOT NULL)
                                                                                         AS active_participants,
    (SELECT COUNT(*) FROM pbi_fact_roster WHERE is_active = 1)                           AS active_rosters,
    (SELECT COUNT(*) FROM pbi_fact_roster WHERE is_scheduled = 1)                        AS scheduled_rosters,
    -- ---- pipeline: estimates (never added to actuals) ----------------------------
    (SELECT COUNT(*) FROM pbi_fact_opportunity WHERE is_active = 1)                      AS active_opportunities,
    (SELECT COUNT(*) FROM pbi_fact_opportunity WHERE is_active = 1 AND priority = 'P1')  AS p1_open,
    (SELECT COUNT(*) FROM pbi_fact_opportunity WHERE is_active = 1 AND priority = 'P2')  AS p2_open,
    (SELECT COUNT(*) FROM pbi_fact_opportunity WHERE is_active = 1 AND priority = 'P3')  AS p3_open,
    (SELECT COUNT(*) FROM pbi_fact_opportunity WHERE found_today = 1)                    AS opportunities_found_today,
    (SELECT COUNT(*) FROM pbi_fact_opportunity WHERE found_this_week = 1)                AS opportunities_found_week,
    (SELECT COUNT(*) FROM pbi_fact_opportunity WHERE found_this_month = 1)               AS opportunities_found_month,
    ROUND(COALESCE((SELECT SUM(est_monthly_value) FROM pbi_fact_opportunity WHERE is_active = 1), 0), 2)
                                                                                         AS pipeline_monthly_value_known,
    (SELECT COUNT(*) FROM pbi_fact_opportunity WHERE is_active = 1 AND est_monthly_value IS NULL)
                                                                                         AS pipeline_value_unknown_count,
    ROUND(COALESCE((SELECT SUM(est_monthly_value) FROM pbi_fact_opportunity
                     WHERE pipeline_horizon = 'NEAR_TERM'), 0), 2)                       AS near_term_monthly_value_known,
    (SELECT COUNT(*) FROM pbi_fact_opportunity WHERE pipeline_horizon = 'NEAR_TERM')     AS near_term_count,
    (SELECT COUNT(*) FROM pbi_fact_opportunity WHERE pipeline_horizon = 'NEAR_TERM' AND est_monthly_value IS NULL)
                                                                                         AS near_term_unknown_count,
    ROUND(COALESCE((SELECT SUM(est_monthly_value) FROM pbi_fact_opportunity
                     WHERE pipeline_horizon = 'LONGER_TERM'), 0), 2)                     AS longer_term_monthly_value_known,
    (SELECT COUNT(*) FROM pbi_fact_opportunity WHERE pipeline_horizon = 'LONGER_TERM')   AS longer_term_count,
    (SELECT COUNT(*) FROM pbi_fact_opportunity WHERE pipeline_horizon = 'LONGER_TERM' AND est_monthly_value IS NULL)
                                                                                         AS longer_term_unknown_count,
    (SELECT ROUND(SUM(weighted_monthly_value), 2) FROM pbi_fact_opportunity WHERE is_active = 1) AS weighted_pipeline_monthly,
    ROUND(COALESCE((SELECT SUM(expected_weekly_hours) FROM pbi_fact_opportunity WHERE is_active = 1), 0), 2)
                                                                                         AS potential_weekly_hours_known,
    -- ---- conversion (rolling 30 days, local dates) --------------------------------
    (SELECT COUNT(*) FROM pbi_fact_reply WHERE is_positive = 1 AND is_estimated_date = 0
       AND received_date >= c.rolling_30_start)
                                                                                         AS positive_conversations_30d,
    (SELECT COUNT(*) FROM pbi_fact_meeting WHERE is_booked_or_held = 1 AND booked_date >= c.rolling_30_start)
                                                                                         AS meetings_booked_30d,
    (SELECT COUNT(*) FROM pbi_fact_referral WHERE received_date >= c.rolling_30_start)   AS referrals_30d,
    (SELECT COUNT(*) FROM pbi_fact_trial_shift WHERE is_scheduled_or_done = 1 AND trial_date >= c.rolling_30_start)
                                                                                         AS trial_shifts_30d,
    (SELECT COUNT(*) FROM pbi_fact_opportunity WHERE reached_positive = 1)               AS positive_conversations_total,
    (SELECT COUNT(*) FROM pbi_fact_meeting WHERE is_booked_or_held = 1)                  AS meetings_total,
    (SELECT COUNT(*) FROM pbi_fact_referral)                                             AS referrals_total,
    (SELECT COUNT(*) FROM pbi_fact_trial_shift WHERE is_scheduled_or_done = 1)           AS trial_shifts_total,
    -- ---- today / action centre ------------------------------------------------------
    (SELECT COUNT(*) FROM pbi_fact_outreach WHERE is_scheduled_today = 1)                AS emails_scheduled_today,
    (SELECT COUNT(*) FROM pbi_fact_follow_up WHERE is_due_today = 1)
      + (SELECT COUNT(*) FROM pbi_fact_outreach WHERE is_follow_up_scheduled_today = 1)  AS followups_due_today,
    (SELECT COUNT(*) FROM pbi_fact_follow_up WHERE is_overdue = 1)                       AS followups_overdue,
    (SELECT COUNT(*) FROM pbi_fact_reply WHERE is_awaiting_response = 1)                 AS replies_awaiting_response,
    (SELECT COUNT(*) FROM pbi_action_centre WHERE category = 'Needs approval')           AS approvals_pending,
    (SELECT COUNT(*) FROM pbi_fact_meeting WHERE is_today = 1)                           AS meetings_today,
    (SELECT COUNT(*) FROM pbi_fact_meeting WHERE is_upcoming = 1)                        AS meetings_upcoming,
    (SELECT COUNT(*) FROM pbi_fact_referral WHERE needs_action = 1)                      AS referrals_needing_action,
    (SELECT COUNT(*) FROM pbi_fact_application WHERE needs_completion = 1)               AS applications_to_complete,
    (SELECT COUNT(*) FROM pbi_fact_outreach, v_cfg cfg WHERE is_bounced = 1
       AND bounced_date >= date(c.today, '-' || cfg.recent_bounce_days || ' days'))      AS bounces_recent,
    (SELECT COUNT(*) FROM pbi_fact_agent_health WHERE rag_status = 'RED')                AS agents_red,
    (SELECT COUNT(*) FROM pbi_fact_agent_health WHERE rag_status = 'AMBER')              AS agents_amber,
    (SELECT COUNT(*) FROM pbi_fact_agent_health WHERE rag_status = 'GREEN')              AS agents_green,
    (SELECT COUNT(*) FROM pbi_fact_data_quality WHERE severity = 'ERROR')                AS dq_errors,
    (SELECT COUNT(*) FROM pbi_fact_data_quality WHERE severity = 'WARNING')              AS dq_warnings,
    (SELECT COUNT(*) FROM pbi_action_centre)                                             AS action_items,
    (SELECT COUNT(*) FROM pbi_action_centre WHERE due_status = 'OVERDUE')                AS action_items_overdue,
    -- ---- outreach -------------------------------------------------------------------
    (SELECT COUNT(*) FROM pbi_fact_outreach WHERE is_sent = 1 AND sent_date = c.today)   AS emails_sent_today,
    (SELECT COUNT(*) FROM pbi_fact_outreach WHERE is_sent = 1 AND sent_date >= c.week_start) AS emails_sent_week,
    (SELECT COUNT(*) FROM pbi_fact_outreach WHERE is_sent = 1 AND sent_date >= c.month_start) AS emails_sent_month,
    (SELECT COUNT(*) FROM v_org_activity WHERE is_contacted = 1)                         AS orgs_contacted,
    (SELECT ROUND(1.0 * SUM(has_replied) / NULLIF(SUM(is_contacted), 0), 4) FROM v_org_activity WHERE is_contacted = 1)
                                                                                         AS reply_rate,
    (SELECT ROUND(1.0 * SUM(has_positive) / NULLIF(SUM(is_contacted), 0), 4) FROM v_org_activity WHERE is_contacted = 1)
                                                                                         AS positive_reply_rate,
    (SELECT ROUND(100.0 * COALESCE(SUM(active_hours_from_contacted_orgs), 0)
                  / NULLIF((SELECT COUNT(*) FROM v_org_activity WHERE is_contacted = 1), 0), 2) FROM pbi_fact_roster)
                                                                                         AS hours_per_100_contacted,
    (SELECT ROUND(100.0 * COALESCE(SUM(revenue_from_contacted_orgs), 0)
                  / NULLIF((SELECT COUNT(*) FROM v_org_activity WHERE is_contacted = 1), 0), 2) FROM pbi_fact_revenue)
                                                                                         AS revenue_per_100_contacted,
    -- ---- workforce ------------------------------------------------------------------
    (SELECT COUNT(*) FROM pbi_fact_worker_capacity WHERE is_verified_active = 1)         AS workers_active,
    (SELECT COUNT(*) FROM pbi_fact_worker_capacity WHERE is_available = 1)               AS workers_available,
    ROUND(COALESCE((SELECT SUM(capacity_weekly_hours) FROM pbi_fact_worker_capacity), 0), 2) AS worker_capacity_hours,
    ROUND(COALESCE((SELECT SUM(allocated_weekly_hours) FROM pbi_fact_worker_capacity WHERE is_verified_active = 1), 0), 2)
                                                                                         AS worker_allocated_hours,
    ROUND(COALESCE((SELECT SUM(available_weekly_hours) FROM pbi_fact_worker_capacity), 0), 2) AS worker_available_hours,
    (SELECT ROUND(SUM(allocated_weekly_hours) / NULLIF(SUM(capacity_weekly_hours), 0), 4)
       FROM pbi_fact_worker_capacity WHERE is_verified_active = 1)                       AS worker_utilisation,
    (SELECT COUNT(*) FROM pbi_fact_worker_requirement WHERE is_open = 1)                 AS open_worker_requirements,
    (SELECT COUNT(*) FROM pbi_fact_roster WHERE is_unfilled = 1)                         AS unfilled_rosters,
    (SELECT COUNT(*) FROM workers WHERE status IN ('APPLICANT', 'ONBOARDING'))           AS workers_in_pipeline
FROM v_ctx c
CROSS JOIN v_targets_current t;

-- The journey funnel with stage-to-stage conversion (all time).
CREATE VIEW pbi_funnel AS
SELECT
    s.stage_order,
    s.stage_code,
    s.stage_name,
    (SELECT COUNT(*) FROM pbi_fact_opportunity f WHERE f.stage_order_reached >= s.stage_order) AS opportunities_reached,
    ROUND(1.0 * (SELECT COUNT(*) FROM pbi_fact_opportunity f WHERE f.stage_order_reached >= s.stage_order)
          / NULLIF((SELECT COUNT(*) FROM pbi_fact_opportunity f WHERE f.stage_order_reached >= s.stage_order - 1), 0), 4)
                                                                                         AS conversion_from_previous
FROM ref_pipeline_stage s;

CREATE VIEW pbi_meta AS
SELECT
    c.now_utc                                                            AS as_of_utc,
    c.now_local                                                          AS as_of_local,
    c.today                                                              AS as_of_date,
    c.week_start,
    c.month_start,
    (SELECT MAX(name) FROM schema_migrations)                            AS schema_version,
    (SELECT COUNT(*) FROM organisations WHERE merged_into_id IS NULL)    AS organisations,
    (SELECT COUNT(*) FROM opportunities)                                 AS opportunities,
    (SELECT COUNT(*) FROM outreach)                                      AS outreach_records,
    (SELECT MAX(run_at) FROM data_quality_runs)                          AS last_dq_run_utc,
    (SELECT MAX(snapshot_date) FROM daily_metrics_snapshot)              AS last_snapshot_date,
    (SELECT value FROM settings WHERE key = 'business.name')             AS business_name
FROM v_ctx c;

CREATE VIEW pbi_daily_brief AS
SELECT brief_date, generated_at, generator, brief_markdown
FROM daily_briefs
WHERE brief_date = (SELECT MAX(brief_date) FROM daily_briefs);

-- The latest brief's 1-3 recommended focus items, one row each (Today page).
CREATE VIEW pbi_daily_focus AS
SELECT
    b.brief_date,
    CAST(f.key AS INTEGER) + 1              AS focus_rank,
    json_extract(f.value, '$.title')        AS focus_title,
    json_extract(f.value, '$.why')          AS focus_why,
    b.generator
FROM daily_briefs b, json_each(b.brief_json, '$.focus') f
WHERE b.brief_date = (SELECT MAX(brief_date) FROM daily_briefs);
