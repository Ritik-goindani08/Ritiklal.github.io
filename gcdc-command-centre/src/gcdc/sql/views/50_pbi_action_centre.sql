-- =============================================================================
-- 50: Action Centre — one prioritised list of what needs attention today.
--
-- action_score (higher = do first) =
--     priority weight  (P1 300, P2 200, P3 100)
--   + category weight  (referral 60, positive reply 55, meeting today 50,
--                       reply 45, approval 40, agent failure 35, follow-up 30,
--                       agent not reporting 30, application 25,
--                       compliance 22, partnership 20,
--                       P1 stalled 15, bounce 5)
--   + overdue days x 5 (capped at 30 days)
--   + known monthly value / 100 (capped at 50)
-- Nothing here is guessed: unknown potential revenue stays NULL / 'UNKNOWN'.
-- =============================================================================

CREATE VIEW v_action_items AS
-- 1. Participant referrals needing action (closest to revenue)
SELECT
    'REFERRAL-' || p.referral_id                                     AS action_key,
    'Referral'                                                       AS category,
    60                                                               AS category_weight,
    'P1'                                                             AS priority,
    p.referring_organisation_id                                      AS organisation_id,
    p.opportunity_id,
    CASE WHEN p.status = 'NEW' AND p.response_due_at < c.now_utc THEN 'Referral response overdue'
         WHEN p.status = 'NEW' THEN 'New referral — respond'
         ELSE 'Referral in progress — ' || lower(replace(p.status, '_', ' ')) END   AS status_label,
    COALESCE(p.next_action, CASE WHEN p.status = 'NEW' THEN 'Call referrer, confirm hours, rate and start date'
                                 ELSE 'Progress referral ' || p.participant_ref END) AS next_action,
    COALESCE(p.next_action_due, date(p.response_due_at, c.tz_mod))   AS due_date,
    'participant_referrals'                                          AS entity_type,
    p.referral_id                                                    AS entity_id,
    CASE WHEN p.requested_weekly_hours IS NOT NULL AND p.expected_hourly_rate IS NOT NULL
         THEN ROUND(p.requested_weekly_hours * p.expected_hourly_rate * cfg.weeks_per_month, 2) END AS potential_monthly_value,
    p.participant_ref                                                AS reference
FROM participant_referrals p CROSS JOIN v_ctx c CROSS JOIN v_cfg cfg
WHERE p.status IN ('NEW', 'ASSESSING', 'ACCEPTED', 'WORKER_MATCHING')

UNION ALL
-- 2. Replies awaiting Ritik's response
SELECT
    'REPLY-' || r.reply_id,
    CASE WHEN r.classification IN ('POSITIVE', 'REFERRAL') THEN 'Positive reply' ELSE 'Reply awaiting response' END,
    CASE WHEN r.classification IN ('POSITIVE', 'REFERRAL') THEN 55 ELSE 45 END,
    CASE WHEN r.classification IN ('POSITIVE', 'REFERRAL') THEN 'P1' ELSE COALESCE(o.priority, 'P2') END,
    r.organisation_id,
    r.opportunity_id,
    CASE WHEN (julianday(c.now_utc) - julianday(r.received_at)) * 24 > cfg.reply_response_hours
         THEN 'Reply overdue' ELSE 'Reply awaiting response' END,
    'Respond to ' || COALESCE(r.from_email, 'reply') || CASE WHEN r.classification = 'UNCLASSIFIED'
         THEN ' (not yet classified)' ELSE ' (' || lower(r.classification) || ')' END,
    date(r.received_at, c.tz_mod, '+' || CAST(cfg.reply_response_hours AS INTEGER) || ' hours'),
    'replies',
    r.reply_id,
    NULL,
    r.subject
FROM replies r CROSS JOIN v_ctx c CROSS JOIN v_cfg cfg
LEFT JOIN opportunities o ON o.opportunity_id = r.opportunity_id
WHERE r.response_status = 'AWAITING_RESPONSE' AND r.requires_response = 1

UNION ALL
-- 3. Items needing Ritik's approval
SELECT
    'APPROVAL-' || x.outreach_id,
    'Needs approval',
    40,
    COALESCE(o.priority, 'P2'),
    x.organisation_id,
    x.opportunity_id,
    'Awaiting Ritik approval',
    'Approve or edit: ' || COALESCE(x.subject, lower(x.channel) || ' ' || lower(x.touch_type)),
    COALESCE(date(x.scheduled_for, c.tz_mod), c.today),
    'outreach',
    x.outreach_id,
    NULL,
    x.to_email
FROM outreach x CROSS JOIN v_ctx c
LEFT JOIN opportunities o ON o.opportunity_id = x.opportunity_id
WHERE x.status = 'PENDING_APPROVAL'
   OR (x.requires_approval = 1 AND x.approved_at IS NULL AND x.status = 'DRAFT')

UNION ALL
SELECT
    'OPP-APPROVAL-' || o.opportunity_id,
    'Needs approval',
    40,
    COALESCE(o.priority, 'P2'),
    o.organisation_id,
    o.opportunity_id,
    'Awaiting Ritik approval',
    COALESCE(o.next_action, 'Approve opportunity: ' || o.title),
    COALESCE(o.next_action_due, c.today),
    'opportunities',
    o.opportunity_id,
    NULL,
    NULL
FROM opportunities o CROSS JOIN v_ctx c
WHERE o.requires_approval = 1 AND o.status = 'OPEN'

UNION ALL
-- 4. Follow-ups due today or overdue
SELECT
    'FOLLOWUP-' || f.follow_up_id,
    'Follow-up',
    30,
    COALESCE(o.priority, 'P3'),
    f.organisation_id,
    f.opportunity_id,
    CASE WHEN f.due_date < c.today THEN 'Follow-up overdue' ELSE 'Follow-up due today' END,
    COALESCE(f.description, lower(f.action_type) || ' follow-up'),
    f.due_date,
    'follow_ups',
    f.follow_up_id,
    NULL,
    NULL
FROM follow_ups f CROSS JOIN v_ctx c
LEFT JOIN opportunities o ON o.opportunity_id = f.opportunity_id
WHERE f.status = 'OPEN' AND f.due_date <= c.today

UNION ALL
-- 5. Meetings today / upcoming
SELECT
    'MEETING-' || m.meeting_id,
    CASE WHEN date(m.scheduled_start, c.tz_mod) = c.today THEN 'Meeting today' ELSE 'Upcoming meeting' END,
    CASE WHEN date(m.scheduled_start, c.tz_mod) = c.today THEN 50 ELSE 20 END,
    CASE WHEN date(m.scheduled_start, c.tz_mod) = c.today THEN 'P1' ELSE COALESCE(o.priority, 'P2') END,
    m.organisation_id,
    m.opportunity_id,
    CASE WHEN date(m.scheduled_start, c.tz_mod) = c.today
         THEN 'Meeting today ' || strftime('%H:%M', m.scheduled_start, c.tz_mod)
         ELSE 'Meeting ' || date(m.scheduled_start, c.tz_mod) END,
    COALESCE(m.next_step, 'Prepare: ' || lower(replace(m.meeting_type, '_', ' ')) || ' meeting'),
    date(m.scheduled_start, c.tz_mod),
    'meetings',
    m.meeting_id,
    NULL,
    m.location
FROM meetings m CROSS JOIN v_ctx c CROSS JOIN v_cfg cfg
LEFT JOIN opportunities o ON o.opportunity_id = m.opportunity_id
WHERE m.status IN ('BOOKED', 'RESCHEDULED')
  AND date(m.scheduled_start, c.tz_mod) BETWEEN c.today AND date(c.today, '+' || cfg.upcoming_meeting_days || ' days')

UNION ALL
-- 6. Applications / forms requiring completion
SELECT
    'APPLICATION-' || a.application_id,
    'Application to complete',
    25,
    CASE COALESCE(a.priority_tier, 3) WHEN 1 THEN 'P1' WHEN 2 THEN 'P2' ELSE 'P3' END,
    a.organisation_id,
    NULL,
    CASE a.status WHEN 'MORE_INFO_REQUESTED' THEN 'More information requested'
                  WHEN 'IN_PROGRESS' THEN 'Application in progress' ELSE 'Application not started' END,
    COALESCE(a.next_action, 'Complete ' || lower(replace(COALESCE(a.application_method, 'application'), '_', ' '))
             || COALESCE(': ' || a.application_url, '')),
    a.next_action_due,
    'associate_provider_applications',
    a.application_id,
    NULL,
    a.application_url
FROM associate_provider_applications a
WHERE a.requires_ritik = 1 AND a.status IN ('NOT_STARTED', 'IN_PROGRESS', 'MORE_INFO_REQUESTED')

UNION ALL
-- 7. Partnership next actions that are due
SELECT
    'PARTNERSHIP-' || p.partnership_id,
    'Partnership',
    20,
    CASE COALESCE(p.priority_tier, 2) WHEN 1 THEN 'P1' WHEN 2 THEN 'P2' ELSE 'P3' END,
    p.organisation_id,
    p.opportunity_id,
    'Partnership action due',
    p.next_action,
    p.next_action_due,
    'partnerships',
    p.partnership_id,
    NULL,
    p.partnership_type
FROM partnerships p CROSS JOIN v_ctx c
WHERE p.status = 'ACTIVE' AND p.next_action IS NOT NULL AND p.next_action_due <= c.today

UNION ALL
-- 8. Recent bounces not yet replaced by a working contact
SELECT
    'BOUNCE-' || x.outreach_id,
    'Bounced email',
    5,
    COALESCE(o.priority, 'P3'),
    x.organisation_id,
    x.opportunity_id,
    'Email bounced',
    'Find a working contact for this organisation (' || COALESCE(x.to_email, '?') || ' bounced)',
    date(x.bounced_at, c.tz_mod),
    'outreach',
    x.outreach_id,
    NULL,
    x.bounce_reason
FROM outreach x CROSS JOIN v_ctx c CROSS JOIN v_cfg cfg
LEFT JOIN opportunities o ON o.opportunity_id = x.opportunity_id
WHERE x.status = 'BOUNCED'
  AND x.bounced_at >= date(c.today, '-' || cfg.recent_bounce_days || ' days')
  AND NOT EXISTS (SELECT 1 FROM outreach y WHERE y.organisation_id = x.organisation_id
                  AND y.status = 'SENT' AND y.sent_at > x.bounced_at)

UNION ALL
-- 9. Agents that have run and are failing / stuck / far overdue
SELECT
    'AGENT-' || h.agent_code,
    'Agent failure',
    35,
    'P2',
    NULL,
    NULL,
    'Agent ' || h.rag_status,
    h.agent_name || ': ' || h.rag_reason,
    c.today,
    'agents',
    NULL,
    NULL,
    h.agent_code
FROM v_agent_health h CROSS JOIN v_ctx c
WHERE h.rag_status = 'RED' AND h.last_attempted_run IS NOT NULL

UNION ALL
-- 9b. Agents that have never reported to the central database (one line)
SELECT
    'AGENT-NOT-REPORTING',
    'Agent not reporting',
    30,
    'P2',
    NULL,
    NULL,
    a.n || ' agent(s) not reporting to the central database',
    'Connect to gcdc ingest / gcdc run: ' || a.names,
    c.today,
    'agents',
    NULL,
    NULL,
    NULL
FROM (SELECT COUNT(*) AS n, group_concat(agent_name, ', ') AS names
        FROM v_agent_health WHERE rag_status = 'RED' AND last_attempted_run IS NULL) a
CROSS JOIN v_ctx c
WHERE a.n > 0

UNION ALL
-- 10. P1 opportunities with no next step (or an overdue one)
SELECT
    'P1-' || o.opportunity_id,
    'P1 opportunity',
    15,
    'P1',
    o.organisation_id,
    o.opportunity_id,
    CASE WHEN o.next_action IS NULL THEN 'P1 has no next action' ELSE 'P1 next action overdue' END,
    COALESCE(o.next_action, 'Decide the next step for ' || o.title),
    COALESCE(o.next_action_due, c.today),
    'opportunities',
    o.opportunity_id,
    v.est_monthly_value,
    NULL
FROM opportunities o
JOIN v_opportunity_value v ON v.opportunity_id = o.opportunity_id
CROSS JOIN v_ctx c
WHERE o.priority = 'P1' AND v.is_active = 1
  AND (o.next_action IS NULL OR o.next_action_due IS NULL OR o.next_action_due < c.today)
  AND o.requires_approval = 0
  -- a scheduled follow-up or send is a next step
  AND NOT EXISTS (SELECT 1 FROM follow_ups f WHERE f.status = 'OPEN'
                  AND (f.opportunity_id = o.opportunity_id OR f.organisation_id = o.organisation_id))
  AND NOT EXISTS (SELECT 1 FROM outreach x WHERE x.status IN ('SCHEDULED', 'APPROVED', 'PENDING_APPROVAL')
                  AND x.organisation_id = o.organisation_id)

UNION ALL
-- 11. Compliance pack gaps (partners ask for these before approving)
SELECT
    'COMPLIANCE',
    'Compliance pack',
    22,
    'P1',
    NULL,
    NULL,
    'Compliance pack: ' || agg.current_docs || ' of ' || agg.total_docs || ' documents current',
    'Obtain: ' || agg.first_missing,
    agg.today,
    'compliance_documents',
    NULL,
    NULL,
    NULL
FROM (
    SELECT
        c.today,
        SUM(CASE WHEN d.is_held = 1 AND (d.expiry_date IS NULL OR d.expiry_date >= c.today) THEN 1 ELSE 0 END) AS current_docs,
        COUNT(*) AS total_docs,
        (SELECT d2.doc_name FROM compliance_documents d2
          WHERE d2.is_held = 0 OR (d2.expiry_date IS NOT NULL AND d2.expiry_date < c.today)
          ORDER BY d2.sort_order LIMIT 1) AS first_missing
    FROM compliance_documents d CROSS JOIN v_ctx c
    GROUP BY c.today
) agg
WHERE agg.current_docs < agg.total_docs;

CREATE VIEW pbi_action_centre AS
SELECT
    i.action_key,
    i.category,
    i.priority,
    CASE i.priority WHEN 'P1' THEN 1 WHEN 'P2' THEN 2 ELSE 3 END                         AS priority_rank,
    i.organisation_id,
    org.name                                                                            AS organisation_name,
    i.opportunity_id,
    o.title                                                                             AS opportunity_title,
    COALESCE(s.stage_name, '—')                                                         AS current_stage,
    i.next_action,
    COALESCE(o.owner, 'Ritik')                                                          AS owner,
    i.due_date,
    CASE WHEN i.due_date IS NULL THEN 'NO_DATE'
         WHEN i.due_date < c.today THEN 'OVERDUE'
         WHEN i.due_date = c.today THEN 'DUE_TODAY'
         ELSE 'UPCOMING' END                                                            AS due_status,
    CASE WHEN i.due_date < c.today THEN CAST(julianday(c.today) - julianday(i.due_date) AS INTEGER) ELSE 0 END
                                                                                        AS days_overdue,
    COALESCE(i.potential_monthly_value, v.est_monthly_value)                            AS potential_monthly_value,
    CASE WHEN COALESCE(i.potential_monthly_value, v.est_monthly_value) IS NULL THEN 'UNKNOWN'
         ELSE '$' || printf('%,d', CAST(ROUND(COALESCE(i.potential_monthly_value, v.est_monthly_value)) AS INTEGER)) || '/mo'
    END                                                                                 AS potential_revenue_display,
    date(COALESCE(v.last_activity_at, oa.last_activity_at), c.tz_mod)                   AS last_activity_date,
    i.status_label,
    i.reference,
    COALESCE(org.region_code, 'UNKNOWN')                                                AS region_code,
    i.entity_type,
    i.entity_id,
    CASE i.priority WHEN 'P1' THEN 300 WHEN 'P2' THEN 200 ELSE 100 END
      + i.category_weight
      + 5 * MIN(30, MAX(0, CASE WHEN i.due_date < c.today
                                THEN CAST(julianday(c.today) - julianday(i.due_date) AS INTEGER) ELSE 0 END))
      + MIN(50, COALESCE(COALESCE(i.potential_monthly_value, v.est_monthly_value), 0) / 100.0) AS action_score
FROM v_action_items i
CROSS JOIN v_ctx c
LEFT JOIN organisations org ON org.organisation_id = i.organisation_id
LEFT JOIN opportunities o ON o.opportunity_id = i.opportunity_id
LEFT JOIN v_opportunity_value v ON v.opportunity_id = i.opportunity_id
LEFT JOIN ref_pipeline_stage s ON s.stage_code = v.effective_stage_code
LEFT JOIN v_org_activity oa ON oa.organisation_id = i.organisation_id;
