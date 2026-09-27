-- =============================================================================
-- 30: Power BI dimensions. Every pbi_* view is exported to <export dir>/<name>.csv
-- and loaded by the semantic model; nothing else is.
-- =============================================================================

CREATE VIEW pbi_dim_region AS
SELECT region_code, region_name, sort_order, is_core, state
FROM ref_region;

CREATE VIEW pbi_dim_stage AS
SELECT stage_code, stage_name, stage_order, default_probability AS stage_probability, description
FROM ref_pipeline_stage;

CREATE VIEW pbi_dim_opportunity_type AS
SELECT type_code AS opportunity_type_code, type_name AS opportunity_type_name, sort_order
FROM ref_opportunity_type;

CREATE VIEW pbi_dim_source AS
SELECT
    s.source_code,
    s.source_name,
    s.source_group,
    CASE s.source_group
        WHEN 'Google' THEN 1 WHEN 'Provider Website' THEN 2 WHEN 'Facebook' THEN 3 WHEN 'SEEK' THEN 4
        WHEN 'Indeed' THEN 5 WHEN 'Government / Tender' THEN 6 WHEN 'Referral' THEN 7 ELSE 8 END AS source_group_order,
    s.sort_order
FROM ref_opportunity_source s;

CREATE VIEW pbi_dim_service_type AS
SELECT service_type_code, service_type_name, sort_order
FROM ref_service_type;

CREATE VIEW pbi_dim_partnership_stage AS
SELECT stage_code AS partnership_stage_code, stage_name AS partnership_stage_name, stage_order
FROM ref_partnership_stage;

CREATE VIEW pbi_dim_organisation AS
SELECT
    o.organisation_id,
    o.name                                                            AS organisation_name,
    o.org_category,
    CASE o.org_category
        WHEN 'SUPPORT_COORDINATOR' THEN 'Support Coordinator'
        WHEN 'COORDINATE_AND_DELIVER' THEN 'Coordinate & Deliver'
        WHEN 'PROVIDER' THEN 'Provider'
        WHEN 'ALLIED_HEALTH' THEN 'Allied Health'
        WHEN 'PLAN_MANAGER' THEN 'Plan Manager'
        WHEN 'AGED_CARE_PROVIDER' THEN 'Aged Care Provider'
        WHEN 'GOVERNMENT' THEN 'Government'
        WHEN 'HOSPITAL' THEN 'Hospital'
        WHEN 'MARKETPLACE' THEN 'Marketplace'
        WHEN 'INSURER' THEN 'Insurer'
        WHEN 'OTHER' THEN 'Other'
        ELSE 'Unknown' END                                            AS org_category_label,
    o.region_code                                                     AS home_region_code,
    o.suburb,
    o.postcode,
    o.tier,
    o.relationship_status,
    o.evidence_level,
    CASE o.delivers_own_workers WHEN 1 THEN 'Yes' WHEN 0 THEN 'No' ELSE 'Unknown' END AS delivers_own_workers,
    o.website,
    o.phone,
    o.general_email,
    o.abn,
    date(o.first_seen_at, c.tz_mod)                                   AS first_seen_date,
    o.first_seen_source,
    a.is_contacted,
    date(a.first_contacted_at, c.tz_mod)                              AS first_contacted_date,
    date(a.last_activity_at, c.tz_mod)                                AS last_activity_date,
    a.touches_sent,
    a.has_replied,
    a.has_positive,
    a.has_meeting,
    a.has_referral,
    a.has_trial,
    a.has_roster,
    CASE WHEN EXISTS (SELECT 1 FROM suppression s WHERE s.scope = 'ORGANISATION'
                      AND s.organisation_id = o.organisation_id) THEN 1 ELSE 0 END AS is_do_not_contact
FROM organisations o
JOIN v_org_activity a ON a.organisation_id = o.organisation_id
CROSS JOIN v_ctx c
WHERE o.merged_into_id IS NULL;

CREATE VIEW pbi_dim_worker AS
SELECT
    w.worker_id,
    w.worker_ref,
    w.display_name,
    w.status,
    w.employment_type,
    COALESCE(w.gender_for_matching, 'UNDISCLOSED')                    AS gender_for_matching,
    w.home_region_code                                                AS region_code,
    w.home_suburb,
    w.max_weekly_hours,
    w.cap_personal_care,
    w.cap_domestic,
    w.cap_community_access,
    w.cap_complex_support,
    w.cap_manual_handling,
    w.cap_medication,
    w.cap_transport,
    v.is_verified_active,
    NULLIF(v.verification_gaps, '')                                   AS verification_gaps,
    w.ndis_screening_expiry,
    w.first_aid_expiry,
    w.cpr_expiry,
    w.start_date
FROM workers w
JOIN v_worker_verified v ON v.worker_id = w.worker_id;

CREATE VIEW pbi_dim_participant AS
SELECT
    p.participant_id,
    p.participant_ref,
    p.funding_type,
    p.region_code,
    p.suburb,
    p.status,
    p.start_date,
    p.end_date,
    p.referring_organisation_id
FROM participants p;

CREATE VIEW pbi_dim_agent AS
SELECT agent_code, agent_name, description, schedule_description, expected_interval_hours, business_days_only, is_active
FROM agents;
