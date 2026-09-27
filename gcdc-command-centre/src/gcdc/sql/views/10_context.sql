-- =============================================================================
-- Views are rebuilt on every `gcdc migrate`/`refresh` (repeatable, not versioned).
-- 10: runtime context, config values and current targets.
-- =============================================================================

-- "Now" and "today" for every other view. runtime_context.as_of_utc is pinned by
-- the refresh pipeline so a whole export agrees on one moment in time.
CREATE VIEW v_ctx AS
SELECT
    c.now_utc,
    c.tz_mod,
    datetime(c.now_utc, c.tz_mod)                                AS now_local,
    date(c.now_utc, c.tz_mod)                                    AS today,
    date(c.now_utc, c.tz_mod, '-1 day')                          AS yesterday,
    date(c.now_utc, c.tz_mod, 'weekday 0', '-6 days')            AS week_start,
    date(c.now_utc, c.tz_mod, 'start of month')                  AS month_start,
    date(c.now_utc, c.tz_mod, 'start of month', '-1 month')      AS prev_month_start,
    date(c.now_utc, c.tz_mod, '-29 days')                        AS rolling_30_start
FROM (
    SELECT CASE WHEN rc.as_of_utc IS NOT NULL
                 AND rc.pinned_at >= strftime('%Y-%m-%dT%H:%M:%SZ', 'now', '-30 minutes')
                THEN rc.as_of_utc ELSE strftime('%Y-%m-%dT%H:%M:%SZ', 'now') END AS now_utc,
           printf('%+.4f hours', rc.tz_offset_hours)                    AS tz_mod
    FROM runtime_context rc
    WHERE rc.id = 1
) c;

-- Typed config values (synced from config/gcdc.toml) with safe defaults.
CREATE VIEW v_cfg AS
SELECT
    COALESCE((SELECT CAST(value AS REAL)    FROM settings WHERE key = 'revenue.weeks_per_month'), 4.333333)         AS weeks_per_month,
    COALESCE((SELECT value                  FROM settings WHERE key = 'revenue.mrr_rate_bases'), '["APPROVED"]')    AS mrr_rate_bases,
    COALESCE((SELECT value                  FROM settings WHERE key = 'pipeline.near_term_min_stage'), 'MEETING')  AS near_term_min_stage,
    COALESCE((SELECT CAST(value AS INTEGER) FROM settings WHERE key = 'pipeline.use_stage_probabilities'), 1)      AS use_stage_probabilities,
    COALESCE((SELECT CAST(value AS REAL)    FROM settings WHERE key = 'action_centre.referral_response_hours'), 24) AS referral_response_hours,
    COALESCE((SELECT CAST(value AS REAL)    FROM settings WHERE key = 'action_centre.reply_response_hours'), 24)    AS reply_response_hours,
    COALESCE((SELECT CAST(value AS INTEGER) FROM settings WHERE key = 'action_centre.upcoming_meeting_days'), 7)    AS upcoming_meeting_days,
    COALESCE((SELECT CAST(value AS INTEGER) FROM settings WHERE key = 'action_centre.recent_bounce_days'), 7)       AS recent_bounce_days,
    COALESCE((SELECT CAST(value AS REAL)    FROM settings WHERE key = 'agent_health.amber_multiplier'), 1.5)        AS amber_multiplier,
    COALESCE((SELECT CAST(value AS REAL)    FROM settings WHERE key = 'agent_health.red_multiplier'), 3.0)          AS red_multiplier,
    COALESCE((SELECT CAST(value AS INTEGER) FROM settings WHERE key = 'workers.require_ndis_screening'), 1)        AS req_ndis_screening,
    COALESCE((SELECT CAST(value AS INTEGER) FROM settings WHERE key = 'workers.require_first_aid'), 1)             AS req_first_aid,
    COALESCE((SELECT CAST(value AS INTEGER) FROM settings WHERE key = 'workers.require_cpr'), 1)                   AS req_cpr,
    COALESCE((SELECT CAST(value AS INTEGER) FROM settings WHERE key = 'workers.require_blue_card'), 0)             AS req_blue_card,
    COALESCE((SELECT CAST(value AS INTEGER) FROM settings WHERE key = 'workers.require_police_check'), 0)          AS req_police_check,
    COALESCE((SELECT CAST(value AS INTEGER) FROM settings WHERE key = 'business.registered_ndis_provider'), 0)     AS registered_ndis_provider;

CREATE VIEW v_near_term_stage AS
SELECT COALESCE((SELECT s.stage_order FROM ref_pipeline_stage s, v_cfg c WHERE s.stage_code = c.near_term_min_stage), 6)
       AS near_term_min_order;

CREATE VIEW v_targets_current AS
SELECT
    (SELECT t.target_value FROM business_targets t, v_ctx c
      WHERE t.target_code = 'MRR' AND t.effective_from <= c.today
      ORDER BY t.effective_from DESC LIMIT 1)                        AS mrr_target,
    (SELECT t.target_value FROM business_targets t, v_ctx c
      WHERE t.target_code = 'WEEKLY_HOURS_MIN' AND t.effective_from <= c.today
      ORDER BY t.effective_from DESC LIMIT 1)                        AS weekly_hours_min,
    (SELECT t.target_value FROM business_targets t, v_ctx c
      WHERE t.target_code = 'WEEKLY_HOURS_MAX' AND t.effective_from <= c.today
      ORDER BY t.effective_from DESC LIMIT 1)                        AS weekly_hours_max;
