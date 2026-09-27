-- =============================================================================
-- GCDC central business-development database — core schema (migration 0001)
--
-- This database is the single source of truth. Agents write to it through the
-- ingest layer (gcdc.ingest); Power BI only ever reads exports of the pbi_*
-- views. Conventions:
--   * Event timestamps: TEXT, ISO-8601 UTC, e.g. '2026-09-24T23:00:00Z'
--   * Business dates:   TEXT, 'YYYY-MM-DD' in Brisbane local time
--   * Unknown numbers (hours, rates, values) are NULL — never 0, never guessed.
--   * Every event table carries (source_system, source_key) so any agent can
--     re-send the same record without creating a duplicate.
-- =============================================================================

-- ---------------------------------------------------------------------------
-- Configuration & runtime
-- ---------------------------------------------------------------------------
CREATE TABLE settings (
    key         TEXT PRIMARY KEY,
    value       TEXT,
    value_type  TEXT NOT NULL DEFAULT 'text' CHECK (value_type IN ('text','number','bool','json')),
    updated_at  TEXT NOT NULL
);

-- Targets are versioned: a change in config inserts a new row effective from
-- the day it changed, so historical target-vs-actual stays correct.
CREATE TABLE business_targets (
    target_id       INTEGER PRIMARY KEY,
    target_code     TEXT NOT NULL,
    target_value    REAL NOT NULL,
    unit            TEXT NOT NULL,
    description     TEXT,
    effective_from  TEXT NOT NULL,
    source          TEXT NOT NULL DEFAULT 'config',
    created_at      TEXT NOT NULL,
    UNIQUE (target_code, effective_from)
);

-- Single row. as_of_utc NULL means "now"; the refresh pipeline pins it so every
-- view and export in one run agrees on what "today" is. A pin older than 30
-- minutes (pinned_at) is ignored, so a killed run can never freeze "today".
CREATE TABLE runtime_context (
    id               INTEGER PRIMARY KEY CHECK (id = 1),
    as_of_utc        TEXT,
    pinned_at        TEXT,
    tz_offset_hours  REAL NOT NULL DEFAULT 10
);
INSERT INTO runtime_context (id, as_of_utc, pinned_at, tz_offset_hours) VALUES (1, NULL, NULL, 10);

-- ---------------------------------------------------------------------------
-- Reference data (editable lookups; seeded in 0002)
-- ---------------------------------------------------------------------------
CREATE TABLE ref_region (
    region_code  TEXT PRIMARY KEY,
    region_name  TEXT NOT NULL,
    sort_order   INTEGER NOT NULL,
    is_core      INTEGER NOT NULL DEFAULT 1 CHECK (is_core IN (0,1)),
    state        TEXT
);

CREATE TABLE ref_region_alias (
    alias        TEXT PRIMARY KEY COLLATE NOCASE,
    region_code  TEXT NOT NULL REFERENCES ref_region(region_code)
);

CREATE TABLE ref_pipeline_stage (
    stage_code           TEXT PRIMARY KEY,
    stage_name           TEXT NOT NULL,
    stage_order          INTEGER NOT NULL UNIQUE,
    default_probability  REAL CHECK (default_probability IS NULL OR default_probability BETWEEN 0 AND 1),
    description          TEXT
);

CREATE TABLE ref_opportunity_type (
    type_code   TEXT PRIMARY KEY,
    type_name   TEXT NOT NULL,
    sort_order  INTEGER NOT NULL
);

-- source_code is granular (e.g. NDIS_REGISTER); source_group is the reporting
-- bucket shown in Power BI (Google, Provider Website, Facebook, SEEK, Indeed,
-- Government / Tender, Referral, Other).
CREATE TABLE ref_opportunity_source (
    source_code   TEXT PRIMARY KEY,
    source_name   TEXT NOT NULL,
    source_group  TEXT NOT NULL,
    sort_order    INTEGER NOT NULL
);

CREATE TABLE ref_service_type (
    service_type_code  TEXT PRIMARY KEY,
    service_type_name  TEXT NOT NULL,
    sort_order         INTEGER NOT NULL
);

CREATE TABLE ref_partnership_stage (
    stage_code   TEXT PRIMARY KEY,
    stage_name   TEXT NOT NULL,
    stage_order  INTEGER NOT NULL UNIQUE
);

-- ---------------------------------------------------------------------------
-- 1. Organisations (+ identifiers used to stop duplicates)
-- ---------------------------------------------------------------------------
CREATE TABLE organisations (
    organisation_id       INTEGER PRIMARY KEY,
    name                  TEXT NOT NULL,
    legal_name            TEXT,
    name_key              TEXT NOT NULL,
    abn                   TEXT CHECK (abn IS NULL OR (length(abn) = 11 AND abn NOT GLOB '*[^0-9]*')),
    org_category          TEXT NOT NULL DEFAULT 'UNKNOWN' CHECK (org_category IN (
                              'SUPPORT_COORDINATOR','COORDINATE_AND_DELIVER','PROVIDER','ALLIED_HEALTH',
                              'PLAN_MANAGER','AGED_CARE_PROVIDER','GOVERNMENT','HOSPITAL','MARKETPLACE',
                              'INSURER','OTHER','UNKNOWN')),
    website               TEXT,
    domain                TEXT,
    phone                 TEXT,
    general_email         TEXT,
    suburb                TEXT,
    postcode              TEXT,
    state                 TEXT,
    region_code           TEXT NOT NULL DEFAULT 'UNKNOWN' REFERENCES ref_region(region_code),
    tier                  INTEGER,
    delivers_own_workers  INTEGER CHECK (delivers_own_workers IN (0,1)),
    does_coordination     INTEGER CHECK (does_coordination IN (0,1)),
    ndis_registered       INTEGER CHECK (ndis_registered IN (0,1)),
    evidence_level        TEXT NOT NULL DEFAULT 'UNVERIFIED' CHECK (evidence_level IN (
                              'WEBSITE_VERIFIED','GOVT_SOURCE','DIRECT_CONTACT','NAME_INFERRED','UNVERIFIED')),
    relationship_status   TEXT NOT NULL DEFAULT 'PROSPECT' CHECK (relationship_status IN (
                              'PROSPECT','IN_CONVERSATION','PARTNER','CLIENT','DORMANT','DO_NOT_CONTACT','CLOSED')),
    merged_into_id        INTEGER REFERENCES organisations(organisation_id),
    first_seen_at         TEXT NOT NULL,
    first_seen_source     TEXT,
    created_by            TEXT,
    notes                 TEXT,
    created_at            TEXT NOT NULL,
    updated_at            TEXT NOT NULL
);
CREATE INDEX ix_org_name_key ON organisations(name_key);
CREATE INDEX ix_org_region ON organisations(region_code);

-- Any agent that finds an organisation resolves it through these keys first.
-- identifier_type: ABN | DOMAIN | NAME_KEY | EMAIL | EXTERNAL ('system:id')
CREATE TABLE organisation_identifiers (
    identifier_type   TEXT NOT NULL CHECK (identifier_type IN ('ABN','DOMAIN','NAME_KEY','EMAIL','EXTERNAL')),
    identifier_value  TEXT NOT NULL,
    organisation_id   INTEGER NOT NULL REFERENCES organisations(organisation_id),
    source            TEXT,
    created_at        TEXT NOT NULL,
    PRIMARY KEY (identifier_type, identifier_value)
);
CREATE INDEX ix_orgid_org ON organisation_identifiers(organisation_id);

-- ---------------------------------------------------------------------------
-- 2. Contacts
-- ---------------------------------------------------------------------------
CREATE TABLE contacts (
    contact_id                INTEGER PRIMARY KEY,
    organisation_id           INTEGER NOT NULL REFERENCES organisations(organisation_id),
    full_name                 TEXT,
    first_name                TEXT,
    role_title                TEXT,
    email                     TEXT COLLATE NOCASE,
    email_status              TEXT NOT NULL DEFAULT 'UNVERIFIED' CHECK (email_status IN (
                                  'UNVERIFIED','VERIFIED','RISKY','INVALID','BOUNCED')),
    email_provenance          TEXT CHECK (email_provenance IN (
                                  'ORG_WEBSITE','GOVT_REGISTER','DIRECT','DIRECTORY','INFERRED','UNKNOWN')),
    email_verified_at         TEXT,
    email_verification_method TEXT,
    phone                     TEXT,
    is_decision_maker         INTEGER NOT NULL DEFAULT 0 CHECK (is_decision_maker IN (0,1)),
    is_role_address           INTEGER CHECK (is_role_address IN (0,1)),
    source_system             TEXT,
    source_url                TEXT,
    created_by                TEXT,
    created_at                TEXT NOT NULL,
    updated_at                TEXT NOT NULL
);
CREATE UNIQUE INDEX ux_contact_email ON contacts(email) WHERE email IS NOT NULL;
CREATE INDEX ix_contact_org ON contacts(organisation_id);

-- ---------------------------------------------------------------------------
-- 3. Opportunities (+ stage history kept by trigger)
-- ---------------------------------------------------------------------------
CREATE TABLE opportunities (
    opportunity_id         INTEGER PRIMARY KEY,
    organisation_id        INTEGER NOT NULL REFERENCES organisations(organisation_id),
    contact_id             INTEGER REFERENCES contacts(contact_id),
    title                  TEXT NOT NULL,
    opportunity_type_code  TEXT NOT NULL DEFAULT 'OTHER' REFERENCES ref_opportunity_type(type_code),
    source_code            TEXT NOT NULL DEFAULT 'OTHER' REFERENCES ref_opportunity_source(source_code),
    source_detail          TEXT,
    source_url             TEXT,
    region_code            TEXT NOT NULL DEFAULT 'UNKNOWN' REFERENCES ref_region(region_code),
    service_type_code      TEXT REFERENCES ref_service_type(service_type_code),
    priority               TEXT CHECK (priority IN ('P1','P2','P3')),
    score                  REAL,
    stage_code             TEXT NOT NULL DEFAULT 'FOUND' REFERENCES ref_pipeline_stage(stage_code),
    stage_changed_at       TEXT,
    status                 TEXT NOT NULL DEFAULT 'OPEN' CHECK (status IN ('OPEN','WON','LOST','ON_HOLD','DISQUALIFIED')),
    is_verified            INTEGER NOT NULL DEFAULT 0 CHECK (is_verified IN (0,1)),
    verified_at            TEXT,
    discovered_at          TEXT NOT NULL,
    discovered_by          TEXT,
    expected_weekly_hours  REAL CHECK (expected_weekly_hours IS NULL OR expected_weekly_hours >= 0),
    expected_hourly_rate   REAL CHECK (expected_hourly_rate IS NULL OR expected_hourly_rate >= 0),
    rate_basis             TEXT CHECK (rate_basis IN ('APPROVED','QUOTED','EXPECTED','RATE_CARD')),
    participant_ref        TEXT,
    next_action            TEXT,
    next_action_due        TEXT,
    owner                  TEXT NOT NULL DEFAULT 'Ritik',
    requires_approval      INTEGER NOT NULL DEFAULT 0 CHECK (requires_approval IN (0,1)),
    closed_at              TEXT,
    lost_reason            TEXT,
    notes                  TEXT,
    source_system          TEXT,
    source_key             TEXT,
    created_at             TEXT NOT NULL,
    updated_at             TEXT NOT NULL
);
CREATE UNIQUE INDEX ux_opp_source ON opportunities(source_system, source_key) WHERE source_key IS NOT NULL;
CREATE INDEX ix_opp_org ON opportunities(organisation_id);
CREATE INDEX ix_opp_stage ON opportunities(stage_code, status);

-- Every source key that has ever resolved to an opportunity (many keys -> one
-- opportunity), so re-running any agent or import is idempotent even after
-- the opportunity was closed or merged.
CREATE TABLE opportunity_keys (
    source_system   TEXT NOT NULL,
    source_key      TEXT NOT NULL,
    opportunity_id  INTEGER NOT NULL REFERENCES opportunities(opportunity_id),
    created_at      TEXT NOT NULL,
    PRIMARY KEY (source_system, source_key)
);

CREATE TABLE opportunity_stage_history (
    history_id      INTEGER PRIMARY KEY,
    opportunity_id  INTEGER NOT NULL REFERENCES opportunities(opportunity_id),
    from_stage      TEXT,
    to_stage        TEXT NOT NULL,
    changed_at      TEXT NOT NULL,
    changed_by      TEXT
);
CREATE INDEX ix_osh_opp ON opportunity_stage_history(opportunity_id);

CREATE TRIGGER trg_opp_stage_insert AFTER INSERT ON opportunities
BEGIN
    INSERT INTO opportunity_stage_history (opportunity_id, from_stage, to_stage, changed_at, changed_by)
    VALUES (NEW.opportunity_id, NULL, NEW.stage_code,
            COALESCE(NEW.stage_changed_at, NEW.discovered_at), NEW.discovered_by);
END;

CREATE TRIGGER trg_opp_stage_update AFTER UPDATE OF stage_code ON opportunities
WHEN OLD.stage_code IS NOT NEW.stage_code
BEGIN
    INSERT INTO opportunity_stage_history (opportunity_id, from_stage, to_stage, changed_at, changed_by)
    VALUES (NEW.opportunity_id, OLD.stage_code, NEW.stage_code,
            COALESCE(NEW.stage_changed_at, strftime('%Y-%m-%dT%H:%M:%SZ','now')), NULL);
END;

-- ---------------------------------------------------------------------------
-- Agent registry & runs (declared early: other tables reference runs)
-- ---------------------------------------------------------------------------
CREATE TABLE agents (
    agent_code               TEXT PRIMARY KEY,
    agent_name               TEXT NOT NULL,
    description              TEXT,
    schedule_description     TEXT,
    expected_interval_hours  REAL,
    business_days_only       INTEGER NOT NULL DEFAULT 0 CHECK (business_days_only IN (0,1)),
    is_active                INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0,1)),
    owner                    TEXT,
    updated_at               TEXT NOT NULL
);

CREATE TABLE agent_runs (
    run_id              INTEGER PRIMARY KEY,
    agent_code          TEXT NOT NULL REFERENCES agents(agent_code),
    started_at          TEXT NOT NULL,
    finished_at         TEXT,
    status              TEXT NOT NULL DEFAULT 'RUNNING' CHECK (status IN ('RUNNING','SUCCESS','WARNING','FAILED','ABORTED')),
    trigger_type        TEXT NOT NULL DEFAULT 'SCHEDULED' CHECK (trigger_type IN ('SCHEDULED','MANUAL','CATCH_UP')),
    records_processed   INTEGER NOT NULL DEFAULT 0,
    records_created     INTEGER NOT NULL DEFAULT 0,
    records_updated     INTEGER NOT NULL DEFAULT 0,
    records_skipped     INTEGER NOT NULL DEFAULT 0,
    errors_count        INTEGER NOT NULL DEFAULT 0,
    warnings_count      INTEGER NOT NULL DEFAULT 0,
    last_error          TEXT,
    next_scheduled_run  TEXT,
    host                TEXT,
    agent_version       TEXT,
    details_json        TEXT
);
CREATE INDEX ix_runs_agent ON agent_runs(agent_code, started_at);

-- ---------------------------------------------------------------------------
-- 4. Outreach (every outbound touch: email, call, form, message)
-- ---------------------------------------------------------------------------
CREATE TABLE outreach (
    outreach_id          INTEGER PRIMARY KEY,
    organisation_id      INTEGER NOT NULL REFERENCES organisations(organisation_id),
    contact_id           INTEGER REFERENCES contacts(contact_id),
    opportunity_id       INTEGER REFERENCES opportunities(opportunity_id),
    channel              TEXT NOT NULL DEFAULT 'EMAIL' CHECK (channel IN (
                             'EMAIL','PHONE','SMS','LINKEDIN','FACEBOOK','IN_PERSON','WEB_FORM','OTHER')),
    touch_type           TEXT NOT NULL DEFAULT 'FIRST_TOUCH' CHECK (touch_type IN ('FIRST_TOUCH','FOLLOW_UP','REPLY','OTHER')),
    sequence_step        INTEGER,
    campaign_code        TEXT,
    to_email             TEXT COLLATE NOCASE,
    subject              TEXT,
    status               TEXT NOT NULL DEFAULT 'DRAFT' CHECK (status IN (
                             'DRAFT','PENDING_APPROVAL','APPROVED','SCHEDULED','SENT','CANCELLED','BOUNCED','FAILED')),
    requires_approval    INTEGER NOT NULL DEFAULT 0 CHECK (requires_approval IN (0,1)),
    approved_by          TEXT,
    approved_at          TEXT,
    scheduled_for        TEXT,
    sent_at              TEXT,
    bounced_at           TEXT,
    bounce_type          TEXT CHECK (bounce_type IN ('HARD','SOFT')),
    bounce_reason        TEXT,
    cancelled_at         TEXT,
    cancel_reason        TEXT,
    call_outcome         TEXT CHECK (call_outcome IN ('CONNECTED','VOICEMAIL','NO_ANSWER','WRONG_NUMBER')),
    external_message_id  TEXT,
    thread_id            TEXT,
    evidence             TEXT NOT NULL DEFAULT 'AGENT_LOG' CHECK (evidence IN ('MAILBOX','AGENT_LOG','SHEET_PLAN','MANUAL')),
    agent_run_id         INTEGER REFERENCES agent_runs(run_id),
    source_system        TEXT,
    source_key           TEXT,
    created_by           TEXT,
    created_at           TEXT NOT NULL,
    updated_at           TEXT NOT NULL
);
CREATE UNIQUE INDEX ux_outreach_msg ON outreach(external_message_id) WHERE external_message_id IS NOT NULL;
CREATE UNIQUE INDEX ux_outreach_source ON outreach(source_system, source_key) WHERE source_key IS NOT NULL;
CREATE INDEX ix_outreach_org ON outreach(organisation_id);
CREATE INDEX ix_outreach_status ON outreach(status, scheduled_for);
CREATE INDEX ix_outreach_thread ON outreach(thread_id);

-- ---------------------------------------------------------------------------
-- 5. Follow-ups
-- ---------------------------------------------------------------------------
CREATE TABLE follow_ups (
    follow_up_id        INTEGER PRIMARY KEY,
    organisation_id     INTEGER NOT NULL REFERENCES organisations(organisation_id),
    opportunity_id      INTEGER REFERENCES opportunities(opportunity_id),
    contact_id          INTEGER REFERENCES contacts(contact_id),
    outreach_id         INTEGER REFERENCES outreach(outreach_id),
    action_type         TEXT NOT NULL DEFAULT 'EMAIL' CHECK (action_type IN (
                            'EMAIL','CALL','FORM','MEETING_PREP','DOCUMENT','OTHER')),
    description         TEXT,
    due_date            TEXT NOT NULL,
    -- Only a touch sent after this moment completes the follow-up (defaults to
    -- created_at). Importers set it to the touch the follow-up follows.
    satisfied_after     TEXT,
    owner               TEXT NOT NULL DEFAULT 'Ritik',
    status              TEXT NOT NULL DEFAULT 'OPEN' CHECK (status IN ('OPEN','DONE','CANCELLED','SKIPPED')),
    completed_at        TEXT,
    completed_by        TEXT,
    result_outreach_id  INTEGER REFERENCES outreach(outreach_id),
    source_system       TEXT,
    source_key          TEXT,
    created_by          TEXT,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL
);
CREATE UNIQUE INDEX ux_followup_source ON follow_ups(source_system, source_key) WHERE source_key IS NOT NULL;
CREATE INDEX ix_followup_due ON follow_ups(status, due_date);

-- ---------------------------------------------------------------------------
-- 6. Replies (inbound)
-- ---------------------------------------------------------------------------
CREATE TABLE replies (
    reply_id              INTEGER PRIMARY KEY,
    organisation_id       INTEGER NOT NULL REFERENCES organisations(organisation_id),
    contact_id            INTEGER REFERENCES contacts(contact_id),
    opportunity_id        INTEGER REFERENCES opportunities(opportunity_id),
    outreach_id           INTEGER REFERENCES outreach(outreach_id),
    channel               TEXT NOT NULL DEFAULT 'EMAIL' CHECK (channel IN (
                              'EMAIL','PHONE','SMS','LINKEDIN','FACEBOOK','IN_PERSON','WEB_FORM','OTHER')),
    received_at           TEXT NOT NULL,
    -- 1 when only a lower bound is known (e.g. a sheet note "reply received"
    -- with no date); such replies count in the funnel but not in daily trends.
    received_at_is_estimate INTEGER NOT NULL DEFAULT 0 CHECK (received_at_is_estimate IN (0,1)),
    from_email            TEXT COLLATE NOCASE,
    subject               TEXT,
    classification        TEXT NOT NULL DEFAULT 'UNCLASSIFIED' CHECK (classification IN (
                              'POSITIVE','REFERRAL','QUESTION','NEUTRAL','NOT_INTERESTED','UNSUBSCRIBE',
                              'OUT_OF_OFFICE','AUTO_REPLY','WRONG_CONTACT','OTHER','UNCLASSIFIED')),
    requires_response     INTEGER NOT NULL DEFAULT 1 CHECK (requires_response IN (0,1)),
    response_status       TEXT NOT NULL DEFAULT 'AWAITING_RESPONSE' CHECK (response_status IN (
                              'AWAITING_RESPONSE','RESPONDED','NO_RESPONSE_NEEDED')),
    responded_at          TEXT,
    response_outreach_id  INTEGER REFERENCES outreach(outreach_id),
    summary               TEXT,
    classified_by         TEXT,
    external_message_id   TEXT,
    thread_id             TEXT,
    source_system         TEXT,
    source_key            TEXT,
    created_by            TEXT,
    created_at            TEXT NOT NULL,
    updated_at            TEXT NOT NULL
);
CREATE UNIQUE INDEX ux_reply_msg ON replies(external_message_id) WHERE external_message_id IS NOT NULL;
CREATE UNIQUE INDEX ux_reply_source ON replies(source_system, source_key) WHERE source_key IS NOT NULL;
CREATE INDEX ix_reply_org ON replies(organisation_id);

-- ---------------------------------------------------------------------------
-- 7. Meetings
-- ---------------------------------------------------------------------------
CREATE TABLE meetings (
    meeting_id         INTEGER PRIMARY KEY,
    organisation_id    INTEGER NOT NULL REFERENCES organisations(organisation_id),
    contact_id         INTEGER REFERENCES contacts(contact_id),
    opportunity_id     INTEGER REFERENCES opportunities(opportunity_id),
    meeting_type       TEXT NOT NULL DEFAULT 'INTRO' CHECK (meeting_type IN (
                           'INTRO','DISCOVERY','PARTNERSHIP','PARTICIPANT_ASSESSMENT','MEET_AND_GREET','OTHER')),
    scheduled_start    TEXT NOT NULL,
    duration_minutes   INTEGER,
    mode               TEXT CHECK (mode IN ('IN_PERSON','PHONE','VIDEO')),
    location           TEXT,
    status             TEXT NOT NULL DEFAULT 'BOOKED' CHECK (status IN (
                           'PROPOSED','BOOKED','COMPLETED','CANCELLED','NO_SHOW','RESCHEDULED')),
    booked_at          TEXT,
    outcome            TEXT,
    next_step          TEXT,
    calendar_event_id  TEXT,
    source_system      TEXT,
    source_key         TEXT,
    created_by         TEXT,
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL
);
CREATE UNIQUE INDEX ux_meeting_cal ON meetings(calendar_event_id) WHERE calendar_event_id IS NOT NULL;
CREATE UNIQUE INDEX ux_meeting_source ON meetings(source_system, source_key) WHERE source_key IS NOT NULL;

-- ---------------------------------------------------------------------------
-- 8. Participant referrals
--    participant_ref is a pseudonymous code (e.g. 'P-0007'). Never store a
--    participant's full name or clinical details in this database.
-- ---------------------------------------------------------------------------
CREATE TABLE participant_referrals (
    referral_id                INTEGER PRIMARY KEY,
    opportunity_id             INTEGER REFERENCES opportunities(opportunity_id),
    referring_organisation_id  INTEGER REFERENCES organisations(organisation_id),
    referring_contact_id       INTEGER REFERENCES contacts(contact_id),
    participant_ref            TEXT NOT NULL,
    received_at                TEXT NOT NULL,
    funding_type               TEXT NOT NULL DEFAULT 'UNKNOWN' CHECK (funding_type IN (
                                   'NDIS_PLAN_MANAGED','NDIS_SELF_MANAGED','NDIS_AGENCY_MANAGED','SUPPORT_AT_HOME',
                                   'HOME_CARE_PACKAGE','PRIVATE','INSURANCE','OTHER','UNKNOWN')),
    service_type_code          TEXT REFERENCES ref_service_type(service_type_code),
    region_code                TEXT NOT NULL DEFAULT 'UNKNOWN' REFERENCES ref_region(region_code),
    suburb                     TEXT,
    requested_weekly_hours     REAL CHECK (requested_weekly_hours IS NULL OR requested_weekly_hours >= 0),
    expected_hourly_rate       REAL CHECK (expected_hourly_rate IS NULL OR expected_hourly_rate >= 0),
    rate_basis                 TEXT CHECK (rate_basis IN ('APPROVED','QUOTED','EXPECTED','RATE_CARD')),
    requested_schedule         TEXT,
    worker_gender_requirement  TEXT CHECK (worker_gender_requirement IN ('ANY','FEMALE','MALE')),
    status                     TEXT NOT NULL DEFAULT 'NEW' CHECK (status IN (
                                   'NEW','ASSESSING','ACCEPTED','WORKER_MATCHING','TRIAL_SCHEDULED','CONVERTED',
                                   'DECLINED','LOST','WITHDRAWN')),
    status_changed_at          TEXT,
    response_due_at            TEXT,
    decline_reason             TEXT,
    next_action                TEXT,
    next_action_due            TEXT,
    source_system              TEXT,
    source_key                 TEXT,
    created_by                 TEXT,
    created_at                 TEXT NOT NULL,
    updated_at                 TEXT NOT NULL
);
CREATE UNIQUE INDEX ux_referral_source ON participant_referrals(source_system, source_key) WHERE source_key IS NOT NULL;

-- ---------------------------------------------------------------------------
-- 12. Workers & 13. availability (declared before trial shifts / rosters)
-- ---------------------------------------------------------------------------
CREATE TABLE workers (
    worker_id                 INTEGER PRIMARY KEY,
    worker_ref                TEXT NOT NULL UNIQUE,
    display_name              TEXT NOT NULL,
    status                    TEXT NOT NULL DEFAULT 'APPLICANT' CHECK (status IN (
                                  'APPLICANT','ONBOARDING','ACTIVE','INACTIVE','OFFBOARDED')),
    employment_type           TEXT CHECK (employment_type IN (
                                  'CASUAL','PART_TIME','FULL_TIME','CONTRACTOR','DIRECTOR')),
    -- Recorded only where a participant legitimately requires a matched worker.
    gender_for_matching       TEXT CHECK (gender_for_matching IN ('FEMALE','MALE','NON_BINARY','UNDISCLOSED')),
    home_suburb               TEXT,
    home_region_code          TEXT NOT NULL DEFAULT 'UNKNOWN' REFERENCES ref_region(region_code),
    max_weekly_hours          REAL CHECK (max_weekly_hours IS NULL OR max_weekly_hours >= 0),
    cap_personal_care         INTEGER NOT NULL DEFAULT 0 CHECK (cap_personal_care IN (0,1)),
    cap_domestic              INTEGER NOT NULL DEFAULT 0 CHECK (cap_domestic IN (0,1)),
    cap_community_access      INTEGER NOT NULL DEFAULT 0 CHECK (cap_community_access IN (0,1)),
    cap_complex_support       INTEGER NOT NULL DEFAULT 0 CHECK (cap_complex_support IN (0,1)),
    cap_manual_handling       INTEGER NOT NULL DEFAULT 0 CHECK (cap_manual_handling IN (0,1)),
    cap_medication            INTEGER NOT NULL DEFAULT 0 CHECK (cap_medication IN (0,1)),
    cap_transport             INTEGER NOT NULL DEFAULT 0 CHECK (cap_transport IN (0,1)),
    ndis_screening_verified   INTEGER NOT NULL DEFAULT 0 CHECK (ndis_screening_verified IN (0,1)),
    ndis_screening_expiry     TEXT,
    blue_card_verified        INTEGER NOT NULL DEFAULT 0 CHECK (blue_card_verified IN (0,1)),
    blue_card_expiry          TEXT,
    police_check_verified     INTEGER NOT NULL DEFAULT 0 CHECK (police_check_verified IN (0,1)),
    police_check_date         TEXT,
    first_aid_expiry          TEXT,
    cpr_expiry                TEXT,
    drivers_licence_verified  INTEGER NOT NULL DEFAULT 0 CHECK (drivers_licence_verified IN (0,1)),
    qualification             TEXT,
    qualification_verified    INTEGER NOT NULL DEFAULT 0 CHECK (qualification_verified IN (0,1)),
    verified_by               TEXT,
    verified_at               TEXT,
    start_date                TEXT,
    end_date                  TEXT,
    source_system             TEXT,
    created_by                TEXT,
    created_at                TEXT NOT NULL,
    updated_at                TEXT NOT NULL
);

CREATE TABLE worker_availability (
    availability_id  INTEGER PRIMARY KEY,
    worker_id        INTEGER NOT NULL REFERENCES workers(worker_id),
    day_of_week      INTEGER NOT NULL CHECK (day_of_week BETWEEN 1 AND 7),  -- ISO: 1 = Monday
    start_time       TEXT NOT NULL CHECK (start_time GLOB '[0-2][0-9]:[0-5][0-9]'),
    end_time         TEXT NOT NULL CHECK (end_time GLOB '[0-2][0-9]:[0-5][0-9]'),
    region_code      TEXT REFERENCES ref_region(region_code),
    effective_from   TEXT,
    effective_to     TEXT,
    is_confirmed     INTEGER NOT NULL DEFAULT 0 CHECK (is_confirmed IN (0,1)),
    source_system    TEXT,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL,
    UNIQUE (worker_id, day_of_week, start_time, end_time, effective_from)
);

-- ---------------------------------------------------------------------------
-- 10. Participants & rosters (active recurring work = recurring revenue)
-- ---------------------------------------------------------------------------
CREATE TABLE participants (
    participant_id             INTEGER PRIMARY KEY,
    participant_ref            TEXT NOT NULL UNIQUE,
    referral_id                INTEGER REFERENCES participant_referrals(referral_id),
    referring_organisation_id  INTEGER REFERENCES organisations(organisation_id),
    funding_type               TEXT NOT NULL DEFAULT 'UNKNOWN' CHECK (funding_type IN (
                                   'NDIS_PLAN_MANAGED','NDIS_SELF_MANAGED','NDIS_AGENCY_MANAGED','SUPPORT_AT_HOME',
                                   'HOME_CARE_PACKAGE','PRIVATE','INSURANCE','OTHER','UNKNOWN')),
    region_code                TEXT NOT NULL DEFAULT 'UNKNOWN' REFERENCES ref_region(region_code),
    suburb                     TEXT,
    status                     TEXT NOT NULL DEFAULT 'ACTIVE' CHECK (status IN ('PROSPECTIVE','ACTIVE','PAUSED','ENDED')),
    start_date                 TEXT,
    end_date                   TEXT,
    end_reason                 TEXT,
    source_system              TEXT,
    created_by                 TEXT,
    created_at                 TEXT NOT NULL,
    updated_at                 TEXT NOT NULL
);

CREATE TABLE rosters (
    roster_id           INTEGER PRIMARY KEY,
    roster_ref          TEXT UNIQUE,
    participant_id      INTEGER REFERENCES participants(participant_id),
    organisation_id     INTEGER REFERENCES organisations(organisation_id),   -- paying / partner organisation
    opportunity_id      INTEGER REFERENCES opportunities(opportunity_id),
    referral_id         INTEGER REFERENCES participant_referrals(referral_id),
    service_type_code   TEXT REFERENCES ref_service_type(service_type_code),
    region_code         TEXT NOT NULL DEFAULT 'UNKNOWN' REFERENCES ref_region(region_code),
    primary_worker_id   INTEGER REFERENCES workers(worker_id),
    weekly_hours        REAL CHECK (weekly_hours IS NULL OR weekly_hours >= 0),
    hourly_rate         REAL CHECK (hourly_rate IS NULL OR hourly_rate >= 0),
    rate_basis          TEXT CHECK (rate_basis IN ('APPROVED','QUOTED','EXPECTED')),
    revenue_model       TEXT NOT NULL DEFAULT 'DIRECT' CHECK (revenue_model IN (
                            'DIRECT','SUBCONTRACT','ASSOCIATE_PROVIDER','PRIVATE')),
    includes_weekday    INTEGER NOT NULL DEFAULT 0 CHECK (includes_weekday IN (0,1)),
    includes_weekend    INTEGER NOT NULL DEFAULT 0 CHECK (includes_weekend IN (0,1)),
    includes_evening    INTEGER NOT NULL DEFAULT 0 CHECK (includes_evening IN (0,1)),
    includes_overnight  INTEGER NOT NULL DEFAULT 0 CHECK (includes_overnight IN (0,1)),
    schedule_text       TEXT,
    start_date          TEXT,
    end_date            TEXT,
    status              TEXT NOT NULL DEFAULT 'ACTIVE' CHECK (status IN ('SCHEDULED','ACTIVE','PAUSED','ENDED')),
    source_system       TEXT,
    created_by          TEXT,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    CHECK (participant_id IS NOT NULL OR organisation_id IS NOT NULL)
);
CREATE INDEX ix_roster_status ON rosters(status);

-- ---------------------------------------------------------------------------
-- 9. Trial shifts
-- ---------------------------------------------------------------------------
CREATE TABLE trial_shifts (
    trial_shift_id   INTEGER PRIMARY KEY,
    referral_id      INTEGER REFERENCES participant_referrals(referral_id),
    opportunity_id   INTEGER REFERENCES opportunities(opportunity_id),
    organisation_id  INTEGER REFERENCES organisations(organisation_id),
    participant_ref  TEXT,
    worker_id        INTEGER REFERENCES workers(worker_id),
    scheduled_start  TEXT NOT NULL,
    hours            REAL CHECK (hours IS NULL OR hours >= 0),
    hourly_rate      REAL CHECK (hourly_rate IS NULL OR hourly_rate >= 0),
    is_billable      INTEGER CHECK (is_billable IN (0,1)),
    status           TEXT NOT NULL DEFAULT 'SCHEDULED' CHECK (status IN ('SCHEDULED','COMPLETED','CANCELLED','NO_SHOW')),
    outcome          TEXT NOT NULL DEFAULT 'PENDING' CHECK (outcome IN ('PENDING','CONVERTED','NOT_CONVERTED')),
    feedback         TEXT,
    roster_id        INTEGER REFERENCES rosters(roster_id),
    source_system    TEXT,
    source_key       TEXT,
    created_by       TEXT,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);
CREATE UNIQUE INDEX ux_trial_source ON trial_shifts(source_system, source_key) WHERE source_key IS NOT NULL;

-- ---------------------------------------------------------------------------
-- 11. Revenue — ACTUALS ONLY (invoices / payments). Pipeline estimates live on
--     opportunities and referrals and are never mixed into this table.
-- ---------------------------------------------------------------------------
CREATE TABLE revenue_transactions (
    revenue_id            INTEGER PRIMARY KEY,
    invoice_number        TEXT,
    invoice_date          TEXT NOT NULL,
    service_period_start  TEXT,
    service_period_end    TEXT,
    organisation_id       INTEGER REFERENCES organisations(organisation_id),
    participant_id        INTEGER REFERENCES participants(participant_id),
    roster_id             INTEGER REFERENCES rosters(roster_id),
    opportunity_id        INTEGER REFERENCES opportunities(opportunity_id),
    service_type_code     TEXT REFERENCES ref_service_type(service_type_code),
    region_code           TEXT REFERENCES ref_region(region_code),
    hours                 REAL,
    hourly_rate           REAL,
    amount_ex_gst         REAL NOT NULL,
    gst_amount            REAL NOT NULL DEFAULT 0,
    amount_paid           REAL NOT NULL DEFAULT 0,
    status                TEXT NOT NULL DEFAULT 'INVOICED' CHECK (status IN (
                              'INVOICED','PART_PAID','PAID','VOID','WRITTEN_OFF')),
    paid_date             TEXT,
    payer_type            TEXT CHECK (payer_type IN (
                              'PLAN_MANAGER','PARTICIPANT','PROVIDER','NDIA','GOVERNMENT','PRIVATE','INSURER','OTHER')),
    source_system         TEXT NOT NULL DEFAULT 'MANUAL',
    source_key            TEXT,
    notes                 TEXT,
    created_by            TEXT,
    created_at            TEXT NOT NULL,
    updated_at            TEXT NOT NULL
);
CREATE UNIQUE INDEX ux_revenue_source ON revenue_transactions(source_system, source_key) WHERE source_key IS NOT NULL;
CREATE INDEX ix_revenue_dates ON revenue_transactions(invoice_date, paid_date);

-- Open worker requirements (shifts / referrals we cannot yet staff)
CREATE TABLE worker_requirements (
    requirement_id      INTEGER PRIMARY KEY,
    referral_id         INTEGER REFERENCES participant_referrals(referral_id),
    roster_id           INTEGER REFERENCES rosters(roster_id),
    opportunity_id      INTEGER REFERENCES opportunities(opportunity_id),
    organisation_id     INTEGER REFERENCES organisations(organisation_id),
    region_code         TEXT NOT NULL DEFAULT 'UNKNOWN' REFERENCES ref_region(region_code),
    service_type_code   TEXT REFERENCES ref_service_type(service_type_code),
    required_capability TEXT,
    gender_requirement  TEXT CHECK (gender_requirement IN ('ANY','FEMALE','MALE')),
    weekly_hours        REAL,
    needs_weekday       INTEGER NOT NULL DEFAULT 0 CHECK (needs_weekday IN (0,1)),
    needs_weekend       INTEGER NOT NULL DEFAULT 0 CHECK (needs_weekend IN (0,1)),
    needs_evening       INTEGER NOT NULL DEFAULT 0 CHECK (needs_evening IN (0,1)),
    needs_overnight     INTEGER NOT NULL DEFAULT 0 CHECK (needs_overnight IN (0,1)),
    needed_by           TEXT,
    status              TEXT NOT NULL DEFAULT 'OPEN' CHECK (status IN ('OPEN','FILLED','CANCELLED')),
    filled_worker_id    INTEGER REFERENCES workers(worker_id),
    notes               TEXT,
    source_system       TEXT,
    source_key          TEXT,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL
);
CREATE UNIQUE INDEX ux_workreq_source ON worker_requirements(source_system, source_key) WHERE source_key IS NOT NULL;

-- ---------------------------------------------------------------------------
-- 14. Partnerships & 15. associate-provider applications
-- ---------------------------------------------------------------------------
CREATE TABLE partnerships (
    partnership_id                INTEGER PRIMARY KEY,
    organisation_id               INTEGER NOT NULL REFERENCES organisations(organisation_id),
    opportunity_id                INTEGER REFERENCES opportunities(opportunity_id),
    partnership_type              TEXT NOT NULL CHECK (partnership_type IN (
                                      'ASSOCIATE_PROVIDER','SUBCONTRACTING','OVERFLOW_PROVIDER','REFERRAL_PARTNER',
                                      'PANEL_SUPPLIER','MARKETPLACE','OTHER')),
    stage_code                    TEXT NOT NULL DEFAULT 'LEAD' REFERENCES ref_partnership_stage(stage_code),
    stage_changed_at              TEXT,
    status                        TEXT NOT NULL DEFAULT 'ACTIVE' CHECK (status IN (
                                      'ACTIVE','ON_HOLD','DECLINED','WITHDRAWN','CLOSED')),
    onboarding_status             TEXT NOT NULL DEFAULT 'NOT_STARTED' CHECK (onboarding_status IN (
                                      'NOT_STARTED','IN_PROGRESS','COMPLETE','BLOCKED')),
    insurance_requirements        TEXT,
    labour_hire_licence_required  TEXT NOT NULL DEFAULT 'UNKNOWN' CHECK (labour_hire_licence_required IN ('YES','NO','UNKNOWN')),
    ndis_registration_required    TEXT NOT NULL DEFAULT 'UNKNOWN' CHECK (ndis_registration_required IN ('YES','NO','UNKNOWN')),
    documents_required_note       TEXT,
    last_communication_at         TEXT,
    next_action                   TEXT,
    next_action_due               TEXT,
    approval_date                 TEXT,
    first_work_received_date      TEXT,
    agreed_hourly_rate            REAL,
    priority_tier                 INTEGER,
    notes                         TEXT,
    source_system                 TEXT,
    created_by                    TEXT,
    created_at                    TEXT NOT NULL,
    updated_at                    TEXT NOT NULL,
    UNIQUE (organisation_id, partnership_type)
);

-- GCDC's own compliance pack (what partners ask for before approving).
CREATE TABLE compliance_documents (
    doc_code      TEXT PRIMARY KEY,
    doc_name      TEXT NOT NULL,
    is_held       INTEGER NOT NULL DEFAULT 0 CHECK (is_held IN (0,1)),
    expiry_date   TEXT,
    evidence_ref  TEXT,
    notes         TEXT,
    sort_order    INTEGER,
    updated_at    TEXT NOT NULL
);

CREATE TABLE partnership_documents (
    partnership_id  INTEGER NOT NULL REFERENCES partnerships(partnership_id),
    doc_code        TEXT NOT NULL REFERENCES compliance_documents(doc_code),
    status          TEXT NOT NULL DEFAULT 'OUTSTANDING' CHECK (status IN (
                        'OUTSTANDING','SUBMITTED','ACCEPTED','NOT_REQUIRED')),
    submitted_at    TEXT,
    updated_at      TEXT NOT NULL,
    PRIMARY KEY (partnership_id, doc_code)
);

CREATE TABLE associate_provider_applications (
    application_id      INTEGER PRIMARY KEY,
    partnership_id      INTEGER REFERENCES partnerships(partnership_id),
    organisation_id     INTEGER NOT NULL REFERENCES organisations(organisation_id),
    application_method  TEXT CHECK (application_method IN (
                            'ONLINE_FORM','PORTAL','EMAIL','PHONE','TENDER_PORTAL','OTHER')),
    application_url     TEXT,
    status              TEXT NOT NULL DEFAULT 'NOT_STARTED' CHECK (status IN (
                            'NOT_STARTED','IN_PROGRESS','SUBMITTED','UNDER_REVIEW','MORE_INFO_REQUESTED',
                            'APPROVED','REJECTED','WITHDRAWN')),
    requires_ritik      INTEGER NOT NULL DEFAULT 1 CHECK (requires_ritik IN (0,1)),
    priority_tier       INTEGER,
    started_at          TEXT,
    submitted_at        TEXT,
    decision_at         TEXT,
    reference_number    TEXT,
    next_action         TEXT,
    next_action_due     TEXT,
    notes               TEXT,
    source_system       TEXT,
    source_key          TEXT,
    created_by          TEXT,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL
);
CREATE UNIQUE INDEX ux_app_source ON associate_provider_applications(source_system, source_key) WHERE source_key IS NOT NULL;

-- ---------------------------------------------------------------------------
-- 16. Suppression (checked before any outreach is recorded as scheduled/sent)
-- ---------------------------------------------------------------------------
CREATE TABLE suppression (
    suppression_id   INTEGER PRIMARY KEY,
    scope            TEXT NOT NULL CHECK (scope IN ('EMAIL','DOMAIN','ORGANISATION')),
    email            TEXT COLLATE NOCASE,
    domain           TEXT COLLATE NOCASE,
    organisation_id  INTEGER REFERENCES organisations(organisation_id),
    reason           TEXT NOT NULL CHECK (reason IN (
                         'UNSUBSCRIBE','HARD_BOUNCE','COMPLAINT','DO_NOT_CONTACT','NOT_RELEVANT','COMPETITOR',
                         'OUT_OF_AREA','LEGAL','OTHER')),
    source           TEXT,
    notes            TEXT,
    expires_at       TEXT,
    created_by       TEXT,
    created_at       TEXT NOT NULL,
    CHECK ((scope = 'EMAIL' AND email IS NOT NULL)
        OR (scope = 'DOMAIN' AND domain IS NOT NULL)
        OR (scope = 'ORGANISATION' AND organisation_id IS NOT NULL))
);
CREATE UNIQUE INDEX ux_supp_email ON suppression(email) WHERE scope = 'EMAIL';
CREATE UNIQUE INDEX ux_supp_domain ON suppression(domain) WHERE scope = 'DOMAIN';
CREATE UNIQUE INDEX ux_supp_org ON suppression(organisation_id) WHERE scope = 'ORGANISATION';

-- ---------------------------------------------------------------------------
-- 18. Errors / alerts
-- ---------------------------------------------------------------------------
CREATE TABLE alerts (
    alert_id       INTEGER PRIMARY KEY,
    alert_key      TEXT NOT NULL,
    source_type    TEXT NOT NULL CHECK (source_type IN ('AGENT','DATA_QUALITY','BUSINESS_RULE','SYSTEM')),
    agent_code     TEXT REFERENCES agents(agent_code),
    run_id         INTEGER REFERENCES agent_runs(run_id),
    severity       TEXT NOT NULL CHECK (severity IN ('INFO','WARNING','ERROR','CRITICAL')),
    category       TEXT NOT NULL,
    entity_type    TEXT,
    entity_id      INTEGER,
    message        TEXT NOT NULL,
    first_seen_at  TEXT NOT NULL,
    last_seen_at   TEXT NOT NULL,
    occurrences    INTEGER NOT NULL DEFAULT 1,
    status         TEXT NOT NULL DEFAULT 'OPEN' CHECK (status IN ('OPEN','ACKNOWLEDGED','RESOLVED')),
    resolved_at    TEXT,
    resolved_by    TEXT
);
CREATE UNIQUE INDEX ux_alert_open ON alerts(alert_key) WHERE status <> 'RESOLVED';

-- ---------------------------------------------------------------------------
-- Data quality results (latest run is what Power BI shows; history retained)
-- ---------------------------------------------------------------------------
CREATE TABLE data_quality_runs (
    check_run_id    INTEGER PRIMARY KEY,
    run_at          TEXT NOT NULL,
    checks_run      INTEGER NOT NULL,
    issues_total    INTEGER NOT NULL,
    errors_total    INTEGER NOT NULL,
    warnings_total  INTEGER NOT NULL
);

CREATE TABLE data_quality_results (
    result_id        INTEGER PRIMARY KEY,
    check_run_id     INTEGER NOT NULL REFERENCES data_quality_runs(check_run_id),
    check_code       TEXT NOT NULL,
    severity         TEXT NOT NULL CHECK (severity IN ('INFO','WARNING','ERROR')),
    entity_type      TEXT,
    entity_id        INTEGER,
    organisation_id  INTEGER,
    detail           TEXT
);
CREATE INDEX ix_dq_run ON data_quality_results(check_run_id);

-- ---------------------------------------------------------------------------
-- 19. Daily metrics snapshot — append-only history. Triggers below make it
--     impossible to UPDATE or DELETE a snapshot once written.
-- ---------------------------------------------------------------------------
CREATE TABLE daily_metrics_snapshot (
    snapshot_date                   TEXT PRIMARY KEY,
    captured_at_utc                 TEXT NOT NULL,
    is_catch_up                     INTEGER NOT NULL DEFAULT 0 CHECK (is_catch_up IN (0,1)),
    -- revenue (actuals)
    mrr                             REAL NOT NULL,
    mrr_rosters_rate_unknown        INTEGER NOT NULL,
    weekly_billable_hours           REAL NOT NULL,
    avg_billable_rate               REAL,
    revenue_received_day            REAL NOT NULL,
    revenue_received_mtd            REAL NOT NULL,
    revenue_invoiced_mtd            REAL NOT NULL,
    revenue_target                  REAL,
    revenue_gap                     REAL,
    weekly_hours_target_min         REAL,
    active_participants             INTEGER NOT NULL,
    active_rosters                  INTEGER NOT NULL,
    -- pipeline (estimates — kept in separate columns from actuals)
    active_opportunities            INTEGER NOT NULL,
    p1_count                        INTEGER NOT NULL,
    p2_count                        INTEGER NOT NULL,
    p3_count                        INTEGER NOT NULL,
    pipeline_monthly_value_known    REAL NOT NULL,
    pipeline_value_unknown_count    INTEGER NOT NULL,
    near_term_monthly_value_known   REAL NOT NULL,
    weighted_pipeline_monthly       REAL,
    -- activity on snapshot_date (Brisbane local day)
    opportunities_found             INTEGER NOT NULL,
    emails_sent                     INTEGER NOT NULL,
    first_touch_sent                INTEGER NOT NULL,
    followups_sent                  INTEGER NOT NULL,
    emails_bounced                  INTEGER NOT NULL,
    replies                         INTEGER NOT NULL,
    positive_replies                INTEGER NOT NULL,
    meetings_booked                 INTEGER NOT NULL,
    meetings_held                   INTEGER NOT NULL,
    referrals                       INTEGER NOT NULL,
    trial_shifts                    INTEGER NOT NULL,
    -- cumulative / state
    orgs_contacted_total            INTEGER NOT NULL,
    overdue_followups               INTEGER NOT NULL,
    replies_awaiting_response       INTEGER NOT NULL,
    workers_active                  INTEGER NOT NULL,
    worker_available_hours          REAL NOT NULL,
    worker_allocated_hours          REAL NOT NULL,
    open_worker_requirements        INTEGER NOT NULL,
    dq_errors                       INTEGER NOT NULL,
    agents_red                      INTEGER NOT NULL
);

CREATE TRIGGER trg_snapshot_no_update BEFORE UPDATE ON daily_metrics_snapshot
BEGIN
    SELECT RAISE(ABORT, 'daily_metrics_snapshot is append-only: history cannot be overwritten');
END;

CREATE TRIGGER trg_snapshot_no_delete BEFORE DELETE ON daily_metrics_snapshot
BEGIN
    SELECT RAISE(ABORT, 'daily_metrics_snapshot is append-only: history cannot be deleted');
END;

-- Morning briefs produced by the Daily BI Agent (kept for history).
CREATE TABLE daily_briefs (
    brief_date      TEXT PRIMARY KEY,
    generated_at    TEXT NOT NULL,
    generator       TEXT NOT NULL,
    brief_markdown  TEXT NOT NULL,
    brief_json      TEXT NOT NULL
);
