"""Clearly-fictional demo data for previewing the dashboard and for tests.

Every demo organisation is named "DEMO — ..." and every participant/worker
reference starts with DEMO. Use a separate database:

    GCDC_DB=data/demo.db GCDC_EXPORT_DIR=exports/demo gcdc demo seed
    GCDC_DB=data/demo.db GCDC_EXPORT_DIR=exports/demo gcdc refresh

The seeder refuses to run against a database that holds real organisations.
"""

from __future__ import annotations

import random
import sqlite3
from datetime import date, datetime, time, timedelta

from gcdc.agents import finish_run
from gcdc.config import Config
from gcdc.db import sync_config
from gcdc.ingest import IngestContext, ingest_records
from gcdc.timeutil import local_today, local_tz, to_iso_utc

REGIONS = ["Gold Coast", "Logan", "Brisbane South", "Brisbane", "Redlands", "Tweed"]
TYPES = ["SUPPORT_COORDINATION", "SUBCONTRACTING", "ASSOCIATE_PROVIDER", "SUPPORT_AT_HOME", "ALLIED_HEALTH_REFERRAL",
         "PARTICIPANT_DIRECT", "SIL", "HOSPITAL_DISCHARGE", "TENDER", "WORKFORCE"]
SOURCES = ["GOOGLE", "PROVIDER_WEBSITE", "FACEBOOK", "SEEK", "INDEED", "GOVERNMENT_TENDER", "REFERRAL",
           "NDIS_REGISTER", "CAREVICINITY"]
CATEGORIES = {"SUPPORT_COORDINATION": "SUPPORT_COORDINATOR", "SUBCONTRACTING": "PROVIDER",
              "ASSOCIATE_PROVIDER": "AGED_CARE_PROVIDER", "SUPPORT_AT_HOME": "AGED_CARE_PROVIDER",
              "ALLIED_HEALTH_REFERRAL": "ALLIED_HEALTH", "PARTICIPANT_DIRECT": "OTHER", "SIL": "PROVIDER",
              "HOSPITAL_DISCHARGE": "HOSPITAL", "TENDER": "GOVERNMENT", "WORKFORCE": "PROVIDER"}
NAMES = ["Harbour", "Seabreeze", "Hinterland", "Riverbend", "Coral", "Bayside", "Summit", "Wattle", "Banksia",
         "Kookaburra", "Palm", "Lighthouse", "Horizon", "Sandstone", "Mangrove", "Tallowood", "Ironbark", "Lagoon"]
SUFFIX = ["Support Coordination", "Care Services", "Community Care", "Home Care", "Allied Health", "Disability Services"]


def _local(d: date, hh: int = 9, mm: int = 0, offset: float = 10) -> str:
    return to_iso_utc(datetime.combine(d, time(hh, mm), tzinfo=local_tz(offset)))


def seed_demo(conn: sqlite3.Connection, cfg: Config, *, seed: int = 7, today: date | None = None) -> dict:
    real = conn.execute("SELECT COUNT(*) FROM organisations WHERE name NOT LIKE 'DEMO — %'").fetchone()[0]
    if real:
        raise SystemExit("This database already holds real organisations. Point GCDC_DB at a separate demo "
                         "database, e.g.  GCDC_DB=data/demo.db gcdc demo seed")
    sync_config(conn, cfg)
    rnd = random.Random(seed)
    today = today or local_today(cfg.utc_offset_hours)
    off = cfg.utc_offset_hours
    recs: list[dict] = []
    orgs = []
    for i in range(64):
        otype = TYPES[i % len(TYPES)]
        name = f"DEMO — {NAMES[i % len(NAMES)]} {SUFFIX[(i // len(NAMES)) % len(SUFFIX)]} {i + 1}"
        region = REGIONS[(i * 7) % len(REGIONS)]
        found = today - timedelta(days=rnd.randint(0, 75))
        org = {"name": name, "website": f"demo-org-{i + 1}.example"}
        orgs.append((org, otype, region, found))
        recs.append({"type": "organisation", **org, "region": region, "org_category": CATEGORIES[otype],
                     "evidence_level": "WEBSITE_VERIFIED" if i % 3 else "NAME_INFERRED", "tier": 1 + i % 4})
        known = i % 4 == 0
        recs.append({"type": "opportunity", "organisation": org, "opportunity_type_code": otype,
                     "source_code": SOURCES[(i * 5) % len(SOURCES)], "source_detail": "Demo data",
                     "source_key": f"demo-opp-{i + 1}", "priority": ["P1", "P2", "P3", "P3"][i % 4],
                     "is_verified": int(i % 3 != 0), "discovered_at": _local(found, 8, 0, off),
                     "expected_weekly_hours": [6, 10, 14, 4][i % 4] if known else None,
                     "expected_hourly_rate": [70, 65, 72, 58][i % 4] if known and i % 8 == 0 else None,
                     "rate_basis": "QUOTED" if known and i % 8 == 0 else None,
                     "next_action": "Call the intake manager" if i % 5 == 0 else None,
                     "next_action_due": (today - timedelta(days=i % 3)).isoformat() if i % 5 == 0 else None})
        recs.append({"type": "contact", "organisation": org, "email": f"intake@demo-org-{i + 1}.example",
                     "full_name": f"Demo Contact {i + 1}", "email_status": "VERIFIED" if i % 2 else "UNVERIFIED"})
    # Outreach: first touches for 48 organisations, follow-ups for some, a few bounces and scheduled sends.
    for i, (org, otype, region, found) in enumerate(orgs[:48]):
        sent = max(found, today - timedelta(days=40)) + timedelta(days=rnd.randint(0, 5))
        sent = min(sent, today)
        email = f"intake@demo-org-{i + 1}.example"
        status = "BOUNCED" if i in (7, 19) else "SENT"
        recs.append({"type": "outreach", "organisation": org, "to_email": email, "subject": "Support worker capacity",
                     "status": "SENT", "sent_at": _local(sent, 9, 0, off), "touch_type": "FIRST_TOUCH",
                     "external_message_id": f"<demo-{i}-1@example>", "thread_id": f"demo-thread-{i}",
                     "evidence": "MAILBOX", "campaign_code": "DEMO"})
        if status == "BOUNCED":
            recs.append({"type": "bounce", "to_email": email, "bounced_at": _local(sent, 9, 5, off)})
        elif i % 3 == 0 and sent + timedelta(days=7) <= today:
            recs.append({"type": "outreach", "organisation": org, "to_email": email, "subject": "RE: Support worker capacity",
                         "status": "SENT", "sent_at": _local(sent + timedelta(days=7), 9, 0, off),
                         "touch_type": "FOLLOW_UP", "sequence_step": 2, "external_message_id": f"<demo-{i}-2@example>",
                         "thread_id": f"demo-thread-{i}", "evidence": "MAILBOX"})
        if i % 4 == 1:
            recs.append({"type": "follow_up", "organisation": org, "due_date": (sent + timedelta(days=7)).isoformat(),
                         "action_type": "EMAIL", "description": "Day-7 follow-up", "source_key": f"demo-fu-{i}",
                         "satisfied_after": _local(sent, 10, 0, off)})
    for i, (org, *_rest) in enumerate(orgs[48:54]):
        recs.append({"type": "outreach", "organisation": org, "to_email": f"intake@demo-org-{i + 49}.example",
                     "subject": "Support worker capacity", "status": "SCHEDULED" if i % 2 else "PENDING_APPROVAL",
                     "scheduled_for": _local(today, 9, 0, off) if i < 4 else _local(today + timedelta(days=1), 9, 0, off),
                     "touch_type": "FIRST_TOUCH", "source_key": f"demo-sched-{i}"})
    # Replies: positive / neutral / not interested / awaiting response.
    reply_plan = [(0, "POSITIVE", True), (3, "REFERRAL", False), (6, "QUESTION", True), (9, "NEUTRAL", False),
                  (12, "NOT_INTERESTED", False), (15, "POSITIVE", False), (18, "OUT_OF_OFFICE", False),
                  (21, "POSITIVE", True), (24, "UNCLASSIFIED", True)]
    for idx, cls, awaiting in reply_plan:
        org = orgs[idx][0]
        recs.append({"type": "reply", "from_email": f"intake@demo-org-{idx + 1}.example",
                     "thread_id": f"demo-thread-{idx}", "received_at": _local(today - timedelta(days=1 + idx % 5), 11, 0, off),
                     "classification": cls, "external_message_id": f"<demo-reply-{idx}@example>",
                     "response_status": "AWAITING_RESPONSE" if awaiting else "RESPONDED",
                     "summary": "Demo reply"})
    # Meetings, referrals, trials.
    for n, (idx, delta, status) in enumerate([(0, -6, "COMPLETED"), (3, 0, "BOOKED"), (15, 3, "BOOKED"), (21, -2, "COMPLETED")]):
        recs.append({"type": "meeting", "organisation": orgs[idx][0], "scheduled_start": _local(today + timedelta(days=delta), 14, 0, off),
                     "meeting_type": "DISCOVERY", "status": status, "source_key": f"demo-meet-{n}", "mode": "VIDEO"})
    referrals = [(0, "DEMO-P-001", 8, 70, "NDIS_PLAN_MANAGED", "CONVERTED", -20),
                 (3, "DEMO-P-002", 6, None, "NDIS_SELF_MANAGED", "NEW", 0),
                 (21, "DEMO-P-003", 12, 72, "SUPPORT_AT_HOME", "TRIAL_SCHEDULED", -3),
                 (15, "DEMO-P-004", None, None, "NDIS_AGENCY_MANAGED", "ASSESSING", -1)]
    for idx, ref, hours, rate, funding, status, delta in referrals:
        recs.append({"type": "referral", "organisation": orgs[idx][0], "participant_ref": ref,
                     "received_at": _local(today + timedelta(days=delta), 10, 0, off), "requested_weekly_hours": hours,
                     "expected_hourly_rate": rate, "rate_basis": "QUOTED" if rate else None, "funding_type": funding,
                     "status": status, "region_code": orgs[idx][2], "service_type_code": "PERSONAL_CARE",
                     "source_key": f"demo-ref-{ref}"})
    # Workers (one missing checks) and availability.
    workers = [("DEMO-W-01", "Demo Worker A", "FEMALE", "Gold Coast", True),
               ("DEMO-W-02", "Demo Worker B", "MALE", "Logan", True),
               ("DEMO-W-03", "Demo Worker C", "FEMALE", "Brisbane South", True),
               ("DEMO-W-04", "Demo Worker D", "FEMALE", "Gold Coast", False)]
    for ref, name, gender, region, ok in workers:
        recs.append({"type": "worker", "worker_ref": ref, "display_name": name, "status": "ACTIVE",
                     "gender_for_matching": gender, "home_region_code": region, "max_weekly_hours": 30,
                     "cap_personal_care": 1, "cap_community_access": 1, "cap_domestic": 1,
                     "cap_complex_support": int(ref.endswith("1")), "ndis_screening_verified": int(ok),
                     "ndis_screening_expiry": "2030-06-30", "first_aid_expiry": (today + timedelta(days=200)).isoformat(),
                     "cpr_expiry": (today + timedelta(days=20 if ref.endswith("2") else 150)).isoformat(),
                     "employment_type": "CASUAL", "start_date": (today - timedelta(days=60)).isoformat()})
        for dow, start, end in [(1, "07:00", "15:00"), (3, "07:00", "15:00"), (5, "16:00", "21:00"), (6, "08:00", "14:00"),
                                (7, "22:00", "06:00")][: (5 if ok else 3)]:
            recs.append({"type": "worker_availability", "worker_ref": ref, "day_of_week": dow, "start_time": start,
                         "end_time": end, "is_confirmed": 1})
    # Participants & rosters (one with an approved rate, one quoted, one unknown, one scheduled subcontract).
    recs += [
        {"type": "participant", "participant_ref": "DEMO-P-001", "status": "ACTIVE", "region_code": orgs[0][2],
         "funding_type": "NDIS_PLAN_MANAGED", "organisation": orgs[0][0], "start_date": (today - timedelta(days=18)).isoformat()},
        {"type": "participant", "participant_ref": "DEMO-P-005", "status": "ACTIVE", "region_code": "Logan",
         "funding_type": "PRIVATE", "organisation": orgs[9][0], "start_date": (today - timedelta(days=40)).isoformat()},
        {"type": "participant", "participant_ref": "DEMO-P-006", "status": "ACTIVE", "region_code": "Redlands",
         "funding_type": "SUPPORT_AT_HOME", "organisation": orgs[4][0], "start_date": (today - timedelta(days=10)).isoformat()},
        {"type": "roster", "roster_ref": "DEMO-R-1", "participant_ref": "DEMO-P-001", "organisation": orgs[0][0],
         "weekly_hours": 8, "hourly_rate": 70, "rate_basis": "APPROVED", "status": "ACTIVE", "worker_ref": "DEMO-W-01",
         "service_type_code": "PERSONAL_CARE", "region_code": orgs[0][2], "includes_weekday": 1,
         "start_date": (today - timedelta(days=18)).isoformat()},
        {"type": "roster", "roster_ref": "DEMO-R-2", "participant_ref": "DEMO-P-005", "organisation": orgs[9][0],
         "weekly_hours": 6, "hourly_rate": 65, "rate_basis": "APPROVED", "status": "ACTIVE", "worker_ref": "DEMO-W-02",
         "service_type_code": "COMMUNITY_ACCESS", "region_code": "Logan", "includes_weekend": 1,
         "start_date": (today - timedelta(days=40)).isoformat()},
        {"type": "roster", "roster_ref": "DEMO-R-3", "participant_ref": "DEMO-P-006", "organisation": orgs[4][0],
         "weekly_hours": 4, "status": "ACTIVE", "worker_ref": "DEMO-W-03", "service_type_code": "DOMESTIC",
         "region_code": "Redlands", "includes_weekday": 1, "start_date": (today - timedelta(days=10)).isoformat()},
        {"type": "roster", "roster_ref": "DEMO-R-4", "organisation": orgs[21][0], "weekly_hours": 12, "hourly_rate": 60,
         "rate_basis": "APPROVED", "status": "SCHEDULED", "revenue_model": "SUBCONTRACT", "service_type_code": "PERSONAL_CARE",
         "region_code": orgs[21][2], "includes_evening": 1, "start_date": (today + timedelta(days=5)).isoformat()},
        {"type": "trial_shift", "organisation": orgs[21][0], "participant_ref": "DEMO-P-003",
         "scheduled_start": _local(today + timedelta(days=2), 9, 0, off), "hours": 3, "status": "SCHEDULED",
         "worker_ref": "DEMO-W-01", "source_key": "demo-trial-1"},
        {"type": "trial_shift", "organisation": orgs[0][0], "participant_ref": "DEMO-P-001",
         "scheduled_start": _local(today - timedelta(days=21), 9, 0, off), "hours": 2, "status": "COMPLETED",
         "outcome": "CONVERTED", "worker_ref": "DEMO-W-01", "source_key": "demo-trial-0"},
        {"type": "worker_requirement", "region_code": "Tweed", "service_type_code": "PERSONAL_CARE",
         "gender_requirement": "FEMALE", "weekly_hours": 10, "needs_weekend": 1, "status": "OPEN",
         "required_capability": "Personal care, manual handling", "source_key": "demo-req-1"},
    ]
    # Revenue actuals: weekly invoices for the two approved rosters.
    for w in range(6):
        d = today - timedelta(days=7 * (w + 1))
        for roster, hours, rate in (("DEMO-R-2", 6, 65), ("DEMO-R-1", 8, 70)):
            if roster == "DEMO-R-1" and w > 1:
                continue
            recs.append({"type": "revenue", "invoice_number": f"DEMO-INV-{roster[-1]}-{w}", "invoice_date": d.isoformat(),
                         "service_period_start": (d - timedelta(days=6)).isoformat(), "service_period_end": d.isoformat(),
                         "roster_ref": roster, "hours": hours, "hourly_rate": rate, "amount_ex_gst": hours * rate,
                         "status": "PAID" if w > 0 else "INVOICED",
                         "paid_date": (d + timedelta(days=5)).isoformat() if w > 0 else None, "payer_type": "PLAN_MANAGER"})
    # Partnerships & applications at different stages.
    stages = ["LEAD", "CONTACTED", "INTERESTED", "APPLICATION", "DUE_DILIGENCE", "APPROVED", "AWAITING_WORK", "WORK_RECEIVED"]
    for n, stage in enumerate(stages):
        org = orgs[30 + n][0]
        recs.append({"type": "partnership", "organisation": org, "partnership_type": ["ASSOCIATE_PROVIDER", "SUBCONTRACTING",
                     "OVERFLOW_PROVIDER"][n % 3], "stage_code": stage, "insurance_requirements": "$20m public liability",
                     "labour_hire_licence_required": ["NO", "UNKNOWN", "YES"][n % 3], "ndis_registration_required": "NO",
                     "next_action": "Send compliance pack" if n % 2 else None,
                     "next_action_due": (today - timedelta(days=1)).isoformat() if n % 2 else None,
                     "documents": [{"doc_code": "PUBLIC_LIABILITY", "status": "OUTSTANDING"},
                                   {"doc_code": "RATE_CARD", "status": "SUBMITTED"}]})
        if n in (3, 4):
            recs.append({"type": "application", "organisation": org, "partnership_type": ["ASSOCIATE_PROVIDER",
                         "SUBCONTRACTING", "OVERFLOW_PROVIDER"][n % 3], "status": "IN_PROGRESS" if n == 3 else "SUBMITTED",
                         "application_method": "ONLINE_FORM", "application_url": f"https://demo-org-{31 + n}.example/partners",
                         "priority_tier": 1})
    recs.append({"type": "compliance_document", "doc_code": "RATE_CARD", "is_held": 1})
    recs.append({"type": "compliance_document", "doc_code": "ABN_GST", "is_held": 1})
    ctx = IngestContext(conn, cfg, agent_code="DEMO", source_system="demo")
    conn.execute("BEGIN IMMEDIATE")
    result = ingest_records(ctx, recs)
    conn.execute("COMMIT")
    # Agent run history: healthy, warning, failing and silent agents.
    history = {"OPPORTUNITY_DISCOVERY": "SUCCESS", "OUTREACH": "SUCCESS", "INBOX": "WARNING",
               "EMAIL_VERIFICATION": "FAILED", "PRE_SEND_CHECKER": "SUCCESS", "MAILBOX_SYNC": "SUCCESS"}
    for agent, status in history.items():
        for k in range(5):
            started = datetime.combine(today - timedelta(days=4 - k), time(7, 0), tzinfo=local_tz(off))
            run_id = conn.execute("INSERT INTO agent_runs (agent_code, started_at, status, trigger_type) VALUES (?, ?, "
                                  "'RUNNING', 'SCHEDULED')", (agent, to_iso_utc(started))).lastrowid
            final = status if k == 4 else "SUCCESS"
            finish_run(conn, run_id, status=final, processed=20 + k, created=k, errors=int(final == "FAILED"),
                       last_error="SMTP verification service timed out" if final == "FAILED" else None)
            conn.execute("UPDATE agent_runs SET finished_at = ? WHERE run_id = ?",
                         (to_iso_utc(started + timedelta(minutes=3 + k)), run_id))
    return {"records": result.processed, "failed": result.failed, "errors": result.errors[:5], "stats": result.stats}
