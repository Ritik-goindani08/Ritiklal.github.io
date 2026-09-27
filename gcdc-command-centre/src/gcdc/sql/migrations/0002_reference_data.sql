-- =============================================================================
-- Migration 0002 — reference data. Lookups are rows (not code) so new values
-- can be added without a release. Stage probabilities and agent schedules are
-- overwritten from config/gcdc.toml on every `gcdc config sync`.
-- =============================================================================

INSERT INTO ref_region (region_code, region_name, sort_order, is_core, state) VALUES
    ('GOLD_COAST',     'Gold Coast',              1, 1, 'QLD'),
    ('LOGAN',          'Logan',                   2, 1, 'QLD'),
    ('BRISBANE_SOUTH', 'Brisbane South',          3, 1, 'QLD'),
    ('BRISBANE',       'Brisbane',                4, 1, 'QLD'),
    ('REDLANDS',       'Redlands',                5, 1, 'QLD'),
    ('TWEED_NNSW',     'Tweed / Northern NSW',    6, 1, 'NSW'),
    ('SCENIC_RIM',     'Scenic Rim',              7, 0, 'QLD'),
    ('IPSWICH',        'Ipswich',                 8, 0, 'QLD'),
    ('MORETON_BAY',    'Moreton Bay',             9, 0, 'QLD'),
    ('OTHER',          'Other / outside area',   90, 0, NULL),
    ('UNKNOWN',        'Unknown',                99, 0, NULL);

-- Spellings found in the existing GCDC sheets, plus common variants.
INSERT INTO ref_region_alias (alias, region_code) VALUES
    ('Gold Coast', 'GOLD_COAST'), ('Gold Coast North', 'GOLD_COAST'), ('Gold Coast South', 'GOLD_COAST'),
    ('GC', 'GOLD_COAST'),
    ('Logan', 'LOGAN'), ('Logan / Beenleigh', 'LOGAN'), ('Beenleigh', 'LOGAN'), ('Greater Logan', 'LOGAN'),
    ('Brisbane South', 'BRISBANE_SOUTH'), ('South Brisbane', 'BRISBANE_SOUTH'),
    ('Brisbane', 'BRISBANE'), ('Brisbane City / North / West', 'BRISBANE'), ('Brisbane North', 'BRISBANE'),
    ('Brisbane City', 'BRISBANE'),
    ('Redlands', 'REDLANDS'), ('Redland', 'REDLANDS'), ('Redland City', 'REDLANDS'),
    ('Tweed', 'TWEED_NNSW'), ('Tweed / Northern NSW', 'TWEED_NNSW'), ('Northern NSW', 'TWEED_NNSW'),
    ('Tweed Heads', 'TWEED_NNSW'), ('Ballina / Byron / Lismore', 'TWEED_NNSW'), ('Northern Rivers', 'TWEED_NNSW'),
    ('Scenic Rim', 'SCENIC_RIM'),
    ('Ipswich', 'IPSWICH'),
    ('Moreton Bay', 'MORETON_BAY'),
    ('QLD - regional', 'OTHER'), ('NSW - other', 'OTHER'), ('Townsville', 'OTHER'), ('Rockhampton', 'OTHER'),
    ('National', 'OTHER'), ('Statewide', 'OTHER'),
    ('Unknown', 'UNKNOWN'), ('', 'UNKNOWN');

-- The journey Power BI analyses end to end.
INSERT INTO ref_pipeline_stage (stage_code, stage_name, stage_order, default_probability, description) VALUES
    ('FOUND',     'Opportunity Found',      1, NULL, 'Recorded by discovery or import'),
    ('VERIFIED',  'Verified',               2, NULL, 'Organisation and fit confirmed from evidence'),
    ('CONTACTED', 'Contacted',              3, NULL, 'At least one outbound touch sent'),
    ('REPLIED',   'Replied',                4, NULL, 'Any human reply received'),
    ('POSITIVE',  'Positive Conversation',  5, NULL, 'Reply or call classified positive / referral intent'),
    ('MEETING',   'Meeting',                6, NULL, 'Meeting booked or held'),
    ('REFERRAL',  'Referral',               7, NULL, 'Participant referral or work offer received'),
    ('TRIAL',     'Trial Shift',            8, NULL, 'Trial shift scheduled or delivered'),
    ('ROSTER',    'Recurring Roster',       9, NULL, 'Recurring roster in place (revenue-generating)');

INSERT INTO ref_opportunity_type (type_code, type_name, sort_order) VALUES
    ('SUPPORT_COORDINATION',   'Support Coordination',    1),
    ('SUBCONTRACTING',         'Subcontracting',          2),
    ('ASSOCIATE_PROVIDER',     'Associate Provider',      3),
    ('SIL',                    'SIL',                     4),
    ('SUPPORT_AT_HOME',        'Support at Home',         5),
    ('AGED_CARE',              'Aged Care',               6),
    ('PARTICIPANT_DIRECT',     'Participant Direct',      7),
    ('ALLIED_HEALTH_REFERRAL', 'Allied Health Referral',  8),
    ('HOSPITAL_DISCHARGE',     'Hospital Discharge',      9),
    ('TENDER',                 'Tender',                 10),
    ('WORKFORCE',              'Workforce Opportunity',  11),
    ('OTHER',                  'Other',                  99);

INSERT INTO ref_opportunity_source (source_code, source_name, source_group, sort_order) VALUES
    ('GOOGLE',            'Google search',                        'Google',               1),
    ('PROVIDER_WEBSITE',  'Provider website',                     'Provider Website',     2),
    ('FACEBOOK',          'Facebook',                             'Facebook',             3),
    ('SEEK',              'SEEK',                                 'SEEK',                 4),
    ('INDEED',            'Indeed',                               'Indeed',               5),
    ('GOVERNMENT_TENDER', 'Government portal / tender',           'Government / Tender',  6),
    ('NDIS_REGISTER',     'NDIS provider register',               'Government / Tender',  7),
    ('DOHAC_LIST',        'Dept. of Health aged care list',       'Government / Tender',  8),
    ('NIISQ_REGISTER',    'NIISQ provider register',              'Government / Tender',  9),
    ('REFERRAL',          'Referral / introduction',              'Referral',            10),
    ('INBOUND',           'Inbound enquiry',                      'Referral',            11),
    ('CAREVICINITY',      'CareVicinity marketplace',             'Other',               12),
    ('LINKEDIN',          'LinkedIn',                             'Other',               13),
    ('MANUAL_RESEARCH',   'Manual research / legacy list',        'Other',               14),
    ('OTHER',             'Other',                                'Other',               99);

INSERT INTO ref_service_type (service_type_code, service_type_name, sort_order) VALUES
    ('PERSONAL_CARE',     'Personal Care',        1),
    ('DOMESTIC',          'Domestic Assistance',  2),
    ('COMMUNITY_ACCESS',  'Community Access',     3),
    ('SIL',               'SIL / Overnight',      4),
    ('RESPITE',           'Respite',              5),
    ('COMPLEX_SUPPORT',   'Complex Support',      6),
    ('TRANSPORT',         'Transport',            7),
    ('MIXED',             'Mixed',                8),
    ('OTHER',             'Other',               99);

INSERT INTO ref_partnership_stage (stage_code, stage_name, stage_order) VALUES
    ('LEAD',              'Partnership Lead',   1),
    ('CONTACTED',         'Contacted',          2),
    ('INTERESTED',        'Interested',         3),
    ('APPLICATION',       'Application',        4),
    ('DUE_DILIGENCE',     'Due Diligence',      5),
    ('APPROVED',          'Approved',           6),
    ('AWAITING_WORK',     'Awaiting Work',      7),
    ('WORK_RECEIVED',     'Work Received',      8),
    ('RECURRING_REVENUE', 'Recurring Revenue',  9);

-- GCDC compliance pack (from GCDC_Target_List > Compliance pack). All start as
-- not held; update with `gcdc import canonical --entity compliance_documents`.
INSERT INTO compliance_documents (doc_code, doc_name, is_held, sort_order, notes, updated_at) VALUES
    ('PUBLIC_LIABILITY',    'Public Liability insurance — certificate of currency', 0,  1, 'Usually $10-20m required. The most-asked item.', strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    ('PROF_INDEMNITY',      'Professional Indemnity insurance — certificate',        0,  2, NULL, strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    ('WORKERS_COMP',        'Workers compensation (WorkCover QLD policy)',           0,  3, NULL, strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    ('NDIS_SCREENING',      'NDIS Worker Screening Check — clearance numbers',       0,  4, NULL, strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    ('POLICE_CHECK',        'National Police Check (< 3 years old)',                 0,  5, NULL, strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    ('BLUE_CARD',           'Blue Card / Working With Vulnerable People',            0,  6, NULL, strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    ('FIRST_AID_CPR',       'First Aid + CPR certificates',                          0,  7, NULL, strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    ('QUALIFICATIONS',      'Qualification certificates (Cert III/IV)',              0,  8, NULL, strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    ('ABN_GST',             'ABN + GST registration status',                         0,  9, NULL, strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    ('RATE_CARD',           'Fee schedule / rate card',                              0, 10, NULL, strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    ('INCIDENT_COMPLAINTS', 'Incident management + complaints process',              0, 11, NULL, strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    ('AVAILABILITY',        'Availability statement — workers, days, suburbs',       0, 12, NULL, strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    ('SUBCONTRACT_AGREEMENT','Signed subcontractor / associated provider agreement', 0, 13, NULL, strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    ('DRIVER_VEHICLE',      'Driver licence + vehicle insurance + rego',             0, 14, NULL, strftime('%Y-%m-%dT%H:%M:%SZ','now'));

-- Agent registry. Schedules/intervals are re-synced from config/gcdc.toml.
INSERT INTO agents (agent_code, agent_name, description, expected_interval_hours, business_days_only, updated_at) VALUES
    ('OPPORTUNITY_DISCOVERY', 'Opportunity Discovery Agent', 'Finds new organisations and opportunities',            24, 1, strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    ('OUTREACH',              'Outreach Agent',              'Drafts, schedules and sends outreach',                  24, 1, strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    ('INBOX',                 'Inbox / Reply Agent',         'Reads replies and bounces, classifies, logs follow-ups', 24, 0, strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    ('EMAIL_VERIFICATION',    'Email Verification',          'Checks deliverability of contact emails',              168, 0, strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    ('WEEKLY_PLANNER',        'Weekly Planner',              'Plans the coming week of outreach',                    168, 0, strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    ('PRE_SEND_CHECKER',      'Pre-Send Checker',            'Checks scheduled emails against suppression and rules', 24, 1, strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    ('MAILBOX_SYNC',          'Mailbox Sync',                'Deterministic Outlook sync: sent items, replies, bounces', 24, 0, strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    ('REFRESH_PIPELINE',      'Refresh Pipeline',            'Data quality, snapshots and Power BI export',           24, 0, strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    ('DAILY_BI',              'Daily BI Agent',              'Morning management brief',                              24, 1, strftime('%Y-%m-%dT%H:%M:%SZ','now'));
