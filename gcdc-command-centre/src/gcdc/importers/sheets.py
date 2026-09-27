"""Importers for the existing GCDC Google Sheets (download each as .xlsx / .csv).

Each importer turns a sheet into ingest records (see gcdc.ingest), so every row
goes through the same de-duplication and validation as agent data. Rules:
  * Organisations are matched across all sheets (ABN > domain > email > name).
  * Sheet "Status" columns that were never maintained are ignored; dated
    columns are the evidence. Dates in the past become SENT with
    evidence=SHEET_PLAN (the mailbox sync later confirms them); future dates
    become SCHEDULED.
  * Nothing is invented: unknown hours, rates and reply dates stay unknown.
    A sheet note "reply received" becomes a reply flagged as an estimated date.
  * Dates before business.records_start_date are reported, not imported.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Callable

from gcdc.identity import name_key, normalise_abn, normalise_email, split_emails
from gcdc.importers.common import as_int, col, first_email, read_table, safe_date, yes
from gcdc.ingest import clean


@dataclass
class ImportPlan:
    kind: str
    records: list[dict[str, Any]] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    rows_read: int = 0
    rows_skipped: int = 0

    def add(self, rtype: str, **fields: Any) -> dict[str, Any]:
        rec = {"type": rtype, **{k: v for k, v in fields.items() if v not in (None, "")}}
        self.records.append(rec)
        return rec


@dataclass
class ImportOptions:
    today: date
    records_start: date = date(2026, 1, 1)
    follow_up_days: tuple[int, ...] = (7, 17)
    create_follow_ups: bool = True


def _at9(d: date) -> str:
    return f"{d.isoformat()}T09:00:00+10:00"


def _org_ref(name: str, abn: Any = None, website: Any = None) -> dict[str, Any]:
    ref: dict[str, Any] = {"name": name}
    if normalise_abn(abn):
        ref["abn"] = normalise_abn(abn)
    if website:
        ref["website"] = str(website)
    return ref


def _evidence(value: Any) -> str | None:
    text = str(value or "").lower()
    if "website verified" in text:
        return "WEBSITE_VERIFIED"
    if "inferred" in text:
        return "NAME_INFERRED"
    return None


def _touch(plan: ImportPlan, opts: ImportOptions, *, org: dict, opp: dict, email: str | None, when: Any,
           step: int, campaign: str, subject: Any, source_key: str, row: Any, channel: str = "EMAIL",
           call_outcome: str | None = None) -> date | None:
    """One planned/sent touch from a dated sheet column."""
    d, problem = safe_date(when, not_before=opts.records_start)
    if problem:
        plan.problems.append(f"{plan.kind} row {row}: {problem}")
        return None
    if d is None:
        return None
    if channel == "EMAIL" and not email:
        plan.problems.append(f"{plan.kind} row {row}: touch dated {d} has no email address — not imported")
        return None
    sent = d <= opts.today
    plan.add("outreach", organisation=org, opportunity=opp, channel=channel,
             to_email=email if channel == "EMAIL" else None, subject=subject,
             touch_type="FIRST_TOUCH" if step == 1 else "FOLLOW_UP", sequence_step=step, campaign_code=campaign,
             status="SENT" if sent else "SCHEDULED", sent_at=_at9(d) if sent else None,
             scheduled_for=None if sent else _at9(d), call_outcome=call_outcome, evidence="SHEET_PLAN",
             source_key=source_key)
    return d


# ---------------------------------------------------------------------------
# 1. GCDC_Email_Campaign_Lists — the Outreach Agent's working lists
# ---------------------------------------------------------------------------
CAMPAIGN_TABS = {
    # tab name prefix: (campaign code, org category, opportunity type)
    "support coordinators": ("EMAIL_SC", "SUPPORT_COORDINATOR", "SUPPORT_COORDINATION"),
    "dual": ("EMAIL_DUAL", "COORDINATE_AND_DELIVER", "SUBCONTRACTING"),
    "allied health": ("EMAIL_AH", "ALLIED_HEALTH", "ALLIED_HEALTH_REFERRAL"),
    "plan managers": ("EMAIL_PM", "PLAN_MANAGER", "OTHER"),
    "providers": ("EMAIL_WORKFORCE", "PROVIDER", "SUBCONTRACTING"),
    "needs a look": ("EMAIL_REVIEW", None, "OTHER"),
}


def campaign_lists(path: Path, opts: ImportOptions) -> ImportPlan:
    from gcdc.importers.common import sheet_names
    plan = ImportPlan("campaign_lists")
    for tab in sheet_names(path):
        spec = next((v for k, v in CAMPAIGN_TABS.items() if tab.lower().startswith(k)), None)
        if spec is None:
            continue
        campaign, category, opp_type = spec
        for rec in read_table(path, tab, header_contains=["Company", "Email"]):
            plan.rows_read += 1
            company = col(rec, "Company")
            if not company:
                plan.rows_skipped += 1
                continue
            abn, website = col(rec, "ABN"), col(rec, "Website")
            org = _org_ref(company, abn, website)
            evidence = _evidence(col(rec, "Evidence"))
            tier = as_int(col(rec, "Tier"))
            plan.add("organisation", **org, suburb=col(rec, "Suburb"), region=col(rec, "Region"), tier=tier,
                     phone=col(rec, "Phone"), org_category=category, evidence_level=evidence,
                     delivers_own_workers=yes(col(rec, "Delivers own workers")),
                     does_coordination=yes(col(rec, "Also coordinates")),
                     external_id=f"ndis_register:{normalise_abn(abn)}" if normalise_abn(abn) else None,
                     notes=col(rec, "Notes"))
            verified = evidence == "WEBSITE_VERIFIED"
            priority = "P2" if (tier == 1 and verified and campaign in ("EMAIL_SC", "EMAIL_DUAL")) else "P3"
            opp = {"source_key": f"{campaign}:{normalise_abn(abn) or name_key(company)}",
                   "opportunity_type_code": opp_type}
            plan.add("opportunity", organisation=org, **opp, source_code="NDIS_REGISTER",
                     source_detail=f"NDIS provider register 11 Aug 2026 — campaign list '{tab}'",
                     is_verified=int(verified), priority=priority, priority_mode="raise_only",
                     discovered_at="2026-08-11", notes=col(rec, "Pitch angle"))
            emails = split_emails(col(rec, "Email"))
            for e in emails:
                plan.add("contact", organisation=org, email=e, email_provenance="GOVT_REGISTER")
            email = emails[0] if emails else None
            subject = col(rec, "Suggested subject")
            first = _touch(plan, opts, org=org, opp=opp, email=email, when=col(rec, "Email 1 sent"), step=1,
                           campaign=campaign, subject=subject, source_key=f"{campaign}:{email or name_key(company)}:1",
                           row=rec["_row"])
            second = _touch(plan, opts, org=org, opp=opp, email=email, when=col(rec, "Email 2 sent"), step=2,
                            campaign=campaign, subject=subject, source_key=f"{campaign}:{email or name_key(company)}:2",
                            row=rec["_row"])
            if first and opts.create_follow_ups:
                previous = 0
                for i, days in enumerate(opts.follow_up_days, start=1):
                    plan.add("follow_up", organisation=org, opportunity=opp,
                             action_type="EMAIL", due_date=(first + timedelta(days=days)).isoformat(),
                             satisfied_after=_at9(first + timedelta(days=previous)),
                             description=f"Follow-up {i} (day {days}) — {tab}",
                             status="DONE" if (i == 1 and second) else None,
                             source_key=f"{campaign}:{email or name_key(company)}:fu{i}")
                    previous = days
            replied = col(rec, "Replied?")
            if replied:
                _sheet_reply(plan, org, opp, email, replied, [first, second], rec["_row"])
    return plan


def _sheet_reply(plan: ImportPlan, org: dict, opp: dict, email: str | None, note: Any,
                 touch_dates: list[date | None], row: Any) -> None:
    """A reply known only from a sheet note: dated at the last known touch (a lower bound), flagged estimate."""
    known = [d for d in touch_dates if d]
    if not known:
        plan.problems.append(f"{plan.kind} row {row}: reply noted ({note!r}) but no touch date to anchor it")
        return
    d, explicit = max(known), None
    try:
        from gcdc.timeutil import parse_date
        explicit = parse_date(note)
    except ValueError:
        pass
    plan.add("reply", organisation=org, opportunity=opp, from_email=email,
             received_at=_at9(explicit or d), received_at_is_estimate=0 if explicit else 1,
             classification="UNCLASSIFIED", requires_response=0, response_status="NO_RESPONSE_NEEDED",
             summary=f"Sheet note: {note}", source_key=f"{plan.kind}:{email or org['name']}:reply")


# ---------------------------------------------------------------------------
# 2. GCDC_Coordinator_Emails — 3-touch support coordinator sequence (Aug 2026)
# ---------------------------------------------------------------------------
def coordinator_emails(path: Path, opts: ImportOptions) -> ImportPlan:
    plan = ImportPlan("coordinator_emails")
    for rec in read_table(path, "Emails", header_contains=["Company", "To"]):
        plan.rows_read += 1
        company = col(rec, "Company")
        if not company:
            plan.rows_skipped += 1
            continue
        org = _org_ref(company)
        plan.add("organisation", **org, suburb=col(rec, "Suburb"), region=col(rec, "Region"),
                 tier=as_int(col(rec, "Tier")), org_category="SUPPORT_COORDINATOR",
                 evidence_level=_evidence(col(rec, "Group")))
        opp = {"source_key": f"coordinator:{name_key(company)}", "opportunity_type_code": "SUPPORT_COORDINATION",
               "match_any_type": True}
        plan.add("opportunity", organisation=org, **opp,
                 source_code="NDIS_REGISTER", source_detail="Support coordinator email campaign (Aug 2026)",
                 priority="P3", priority_mode="raise_only", discovered_at="2026-08-11")
        emails = split_emails(col(rec, "To"))
        first_name = col(rec, "First name")
        for i, e in enumerate(emails):
            plan.add("contact", organisation=org, email=e, full_name=first_name if i == 0 else None,
                     email_provenance="ORG_WEBSITE")
        email = emails[0] if emails else None
        subject = col(rec, "Subject")
        dates = []
        for step, column in ((1, "Sent 1"), (2, "Sent 2"), (3, "Sent 3")):
            dates.append(_touch(plan, opts, org=org, opp=opp, email=email,
                                when=col(rec, column), step=step, campaign="EMAIL_SC_AUG",
                                subject=subject if step == 1 else f"RE: {subject}" if subject else None,
                                source_key=f"EMAIL_SC_AUG:{email or name_key(company)}:{step}", row=rec["_row"]))
        note = str(col(rec, "Notes") or "")
        replied = col(rec, "Replied?")
        if replied or re.search(r"\b(repl(y|ied)|received)\b", note, re.IGNORECASE):
            _sheet_reply(plan, org, opp, email, replied or note.strip(), dates, rec["_row"])
        elif note.strip():
            plan.add("organisation", **org, notes=f"Coordinator campaign note: {note.strip()}")
    return plan


# ---------------------------------------------------------------------------
# 3. GCDC_Referral_CRM — call CRM with the real conversation history
# ---------------------------------------------------------------------------
_CRM_TYPE = [
    (r"support coord\. \+ provider", "COORDINATE_AND_DELIVER", "SUPPORT_COORDINATION"),
    (r"plan m", "PLAN_MANAGER", "OTHER"),
    (r"support coordinator|specialist support coord|support coord\.", "SUPPORT_COORDINATOR", "SUPPORT_COORDINATION"),
    (r"^provider", "PROVIDER", "SUBCONTRACTING"),
    (r"local area coordinator", "OTHER", "OTHER"),
    (r"ndis reg\. support coordination", None, "SUPPORT_COORDINATION"),
]
_CRM_STATUS = {
    # status: (channel, call outcome, reply classification or None, opportunity status or None)
    "called - no answer": ("PHONE", "NO_ANSWER", None, None),
    "left voicemail": ("PHONE", "VOICEMAIL", None, None),
    "email sent": ("EMAIL", None, None, None),
    "warm - interested": ("PHONE", "CONNECTED", "POSITIVE", None),
    "trial offered": ("PHONE", "CONNECTED", "POSITIVE", None),
    "trial booked": ("PHONE", "CONNECTED", "POSITIVE", None),
    "nurture": ("PHONE", "CONNECTED", "NEUTRAL", None),
    "not interested": ("PHONE", "CONNECTED", "NOT_INTERESTED", "LOST"),
}


def referral_crm(path: Path, opts: ImportOptions) -> ImportPlan:
    plan = ImportPlan("referral_crm")
    for rec in read_table(path, "CRM", header_contains=["Rank", "Company"]):
        plan.rows_read += 1
        company = col(rec, "Company")
        if not company:
            plan.rows_skipped += 1
            continue
        abn, website = col(rec, "ABN"), col(rec, "Website")
        org = _org_ref(company, abn, website)
        ctype = str(col(rec, "Type") or "").lower()
        category, opp_type = None, "SUPPORT_COORDINATION"
        for pattern, cat, otype in _CRM_TYPE:
            if re.search(pattern, ctype):
                category, opp_type = cat, otype
                break
        plan.add("organisation", **org, suburb=col(rec, "Suburb"), postcode=col(rec, "P/code"),
                 region=col(rec, "Region"), tier=as_int(col(rec, "Tier")), phone=col(rec, "Phone"),
                 org_category=category)
        emails = split_emails(col(rec, "Email"))
        for e in emails:
            plan.add("contact", organisation=org, email=e, email_provenance="GOVT_REGISTER")
        prio = {"call first": "P1", "high": "P2"}.get(str(col(rec, "Priority") or "").strip().lower(), "P3")
        notes = col(rec, "Notes")
        on_hold = bool(notes and re.search(r"out of service area", str(notes), re.IGNORECASE))
        status_text = str(col(rec, "Status") or "").strip().lower()
        mapped = _CRM_STATUS.get(status_text)
        # The CRM's generic "NDIS reg. support coordination" label is the whole register, not a distinct
        # opportunity: attach to the organisation's existing opportunity when it already has one.
        opp = {"source_key": f"referral_crm:{normalise_abn(abn) or name_key(company)}", "opportunity_type_code": opp_type,
               "match_any_type": category is None}
        plan.add("opportunity", organisation=org, **opp,
                 source_code="MANUAL_RESEARCH" if "original" in str(col(rec, "Source") or "").lower() else "NDIS_REGISTER",
                 source_detail=f"Referral CRM v2 — {col(rec, 'Source') or 'unknown source'}",
                 priority=prio, priority_mode="raise_only", score=col(rec, "Score"),
                 owner=col(rec, "Owner") or "Ritik", discovered_at="2026-08-11", notes=notes,
                 status="ON_HOLD" if on_hold else (mapped[3] if mapped else None),
                 lost_reason="Out of service area" if on_hold else (status_text.capitalize() if mapped and mapped[3] else None))
        last, problem = safe_date(col(rec, "Last contact"), not_before=opts.records_start)
        if problem:
            plan.problems.append(f"referral_crm row {rec['_row']}: {problem}")
        if mapped and last:
            channel, outcome, classification, _ = mapped
            email = emails[0] if emails else None
            if channel == "EMAIL" and not email:
                channel, outcome = "PHONE", None
            key = f"referral_crm:{name_key(company)}:{last.isoformat()}"
            plan.add("outreach", organisation=org, opportunity=opp, channel=channel,
                     to_email=email if channel == "EMAIL" else None, touch_type="FIRST_TOUCH", status="SENT",
                     sent_at=_at9(last), call_outcome=outcome, evidence="MANUAL", campaign_code="CALLS_AUG",
                     source_key=key)
            if classification:
                plan.add("reply", organisation=org, opportunity=opp, channel="PHONE",
                         received_at=_at9(last), classification=classification, requires_response=0,
                         response_status="NO_RESPONSE_NEEDED", classified_by="Referral CRM status",
                         summary=f"{col(rec, 'Status')}: {notes or ''}".strip(": "), source_key=key + ":reply")
        elif mapped and not last and not problem:  # a rejected date was already reported above
            plan.problems.append(f"referral_crm row {rec['_row']}: status '{col(rec, 'Status')}' has no Last contact date")
        nxt, problem = safe_date(col(rec, "Next follow-up"), not_before=opts.records_start)
        if nxt and not on_hold and not (mapped and mapped[3] == "LOST"):
            plan.add("follow_up", organisation=org, opportunity=opp,
                     action_type="CALL", due_date=nxt.isoformat(),
                     satisfied_after=_at9(last) if last else None,
                     description=f"Call back ({col(rec, 'Status')})" + (f": {notes}" if notes else ""),
                     owner=col(rec, "Owner") or "Ritik", source_key=f"referral_crm:{name_key(company)}:next")
    return plan


# ---------------------------------------------------------------------------
# 4. GCDC_Support_at_Home_Provider_Database — aged-care associate provider targets
# ---------------------------------------------------------------------------
def sah_provider_db(path: Path, opts: ImportOptions) -> ImportPlan:
    plan = ImportPlan("sah_provider_db")
    for rec in read_table(path, "DATABASE", header_contains=["ID", "Organisation"]):
        plan.rows_read += 1
        sah_id, company = col(rec, "ID"), col(rec, "Organisation (trading name)", "Organisation")
        if not company or not sah_id:
            plan.rows_skipped += 1
            continue
        website = col(rec, "Website")
        org = _org_ref(company, None, website)
        govt = str(col(rec, "Approved home care provider") or "").strip()
        general = first_email(col(rec, "General email"))
        plan.add("organisation", **org, legal_name=col(rec, "Legal entity"), suburb=col(rec, "Office suburb"),
                 postcode=col(rec, "Postcode"), region=col(rec, "Region"), phone=col(rec, "Main phone"),
                 general_email=general, org_category="AGED_CARE_PROVIDER",
                 evidence_level="GOVT_SOURCE" if govt and govt.lower().startswith(("y", "listed")) else
                 ("WEBSITE_VERIFIED" if col(rec, "Website verified") else None),
                 ndis_registered=yes(col(rec, "NDIS")), external_id=f"sah_db:{sah_id}")
        if general:
            plan.add("contact", organisation=org, email=general, email_provenance="ORG_WEBSITE",
                     source_url=col(rec, "Contact page URL"))
        dm_email = first_email(col(rec, "Decision-maker email"))
        dm_name = clean(col(rec, "Decision-maker name"))
        if dm_email or dm_name:
            plan.add("contact", organisation=org, email=dm_email, full_name=dm_name,
                     role_title=col(rec, "Decision-maker role"), is_decision_maker=1, email_provenance="ORG_WEBSITE",
                     source_url=col(rec, "Decision-maker source URL"))
        supplier = first_email(col(rec, "Supplier email"))
        if supplier:
            plan.add("contact", organisation=org, email=supplier, role_title="Supplier / partner intake",
                     email_provenance="ORG_WEBSITE")
        prio = {"A": "P1", "B": "P2", "C": "P3"}.get(str(col(rec, "Priority") or "").strip().upper()[:1])
        sah_status = str(col(rec, "Support at Home status") or "")
        added, _ = safe_date(col(rec, "Date added"), not_before=opts.records_start)
        opp = {"source_key": str(sah_id), "opportunity_type_code": "SUPPORT_AT_HOME"}
        plan.add("opportunity", organisation=org, **opp,
                 source_code="DOHAC_LIST" if govt else "PROVIDER_WEBSITE",
                 source_detail="Support at Home provider database (30 Aug 2026)",
                 source_url=col(rec, "Support at Home evidence URL"),
                 priority=prio, priority_mode="raise_only", score=col(rec, "Priority score"),
                 is_verified=int(sah_status.upper().startswith("VERIFIED")),
                 discovered_at=(added or date(2026, 8, 30)).isoformat(),
                 notes=" | ".join(str(x) for x in (col(rec, "Why GCDC is relevant"), col(rec, "Pitch angle")) if x))
        if yes(col(rec, "Partner/supplier page exists")):
            method = str(col(rec, "Partner intake method") or "")[:240]
            plan.add("partnership", organisation=org, partnership_type="ASSOCIATE_PROVIDER", stage_code="LEAD",
                     next_action=f"Partner intake: {method}" if method else "Use the published partner/supplier intake",
                     documents_required_note=col(rec, "Requirements listed"),
                     notes=col(rec, "Partner/supplier page URL"))
        for step, column in ((1, "First contact date"),):
            _touch(plan, opts, org=org, opp=opp, email=dm_email or general,
                   when=col(rec, column), step=step, campaign="SAH_OUTREACH",
                   subject=col(rec, "Personalised subject line"), source_key=f"sah_db:{sah_id}:{step}", row=rec["_row"])
    try:
        for rec in read_table(path, "SUPPRESSION", header_contains=["Organisation", "Email"]):
            target = str(col(rec, "Email / domain", "Email") or "").strip()
            if "@" in target:
                plan.add("suppression", scope="EMAIL", email=target, reason="DO_NOT_CONTACT",
                         notes=col(rec, "Reason"))
            elif target:
                plan.add("suppression", scope="DOMAIN", domain=target, reason="DO_NOT_CONTACT",
                         notes=col(rec, "Reason"))
    except ValueError:
        pass
    return plan


# ---------------------------------------------------------------------------
# 5. GCDC OUTREACH QUEUE - CLEAN (84 companies) — planned SAH contact tasks
# ---------------------------------------------------------------------------
def outreach_queue(path: Path, opts: ImportOptions) -> ImportPlan:
    plan = ImportPlan("outreach_queue")
    for rec in read_table(path, None if path.suffix == ".csv" else None,
                          header_contains=["Seq", "Organisation"]):
        plan.rows_read += 1
        company = col(rec, "Organisation (one row per company)", "Organisation")
        if not company:
            plan.rows_skipped += 1
            continue
        org = _org_ref(company)
        region = str(col(rec, "Region(s) covered", "Region") or "").split(",")[0]
        phone = str(col(rec, "Phone") or "").split("|")[0].strip()
        plan.add("organisation", **org, region=region, phone=phone if re.search(r"\d{6,}", phone.replace(" ", "")) else None,
                 org_category="AGED_CARE_PROVIDER")
        emails = split_emails(col(rec, "Email (verified only)", "Email"))
        for e in emails:
            plan.add("contact", organisation=org, email=e, email_provenance="ORG_WEBSITE")
        prio = {"A": "P1", "B": "P2", "C": "P3"}.get(str(col(rec, "Priority") or "").strip().upper()[:1])
        opp = {"source_key": f"outreach_queue:{name_key(company)}", "opportunity_type_code": "SUPPORT_AT_HOME"}
        plan.add("opportunity", organisation=org, **opp, source_code="DOHAC_LIST",
                 source_detail="Support at Home outreach queue (clean, 9 Sep 2026)", priority=prio,
                 priority_mode="raise_only", score=col(rec, "Score"), discovered_at="2026-08-30")
        due, problem = safe_date(col(rec, "Contact day"), not_before=opts.records_start)
        if problem:
            plan.problems.append(f"outreach_queue row {rec['_row']}: {problem}")
        if due:
            action = str(col(rec, "Action type") or "")
            plan.add("follow_up", organisation=org, opportunity=opp,
                     action_type="EMAIL" if action.upper().startswith("EMAIL") else "CALL", due_date=due.isoformat(),
                     satisfied_after=_at9(due - timedelta(days=7)),
                     description=f"{action}: {col(rec, 'Subject line to use') or ''}".strip(": "),
                     status="DONE" if yes(col(rec, "Done?")) else None,
                     source_key=f"outreach_queue:{name_key(company)}")
    return plan


# ---------------------------------------------------------------------------
# 6. GCDC_Target_List — associate-provider / panel applications + compliance pack
# ---------------------------------------------------------------------------
_APP_STATUS = {"submitted": "SUBMITTED", "not started": "NOT_STARTED", "in progress": "IN_PROGRESS",
               "approved": "APPROVED", "accepted": "APPROVED", "rejected": "REJECTED", "declined": "REJECTED"}
_DOC_KEYWORDS = [
    ("public liability", "PUBLIC_LIABILITY"), ("professional indemnity", "PROF_INDEMNITY"),
    ("workers comp", "WORKERS_COMP"), ("ndis worker screening", "NDIS_SCREENING"), ("police", "POLICE_CHECK"),
    ("blue card", "BLUE_CARD"), ("first aid", "FIRST_AID_CPR"), ("qualification", "QUALIFICATIONS"),
    ("abn", "ABN_GST"), ("rate card", "RATE_CARD"), ("fee schedule", "RATE_CARD"),
    ("incident", "INCIDENT_COMPLAINTS"), ("availability", "AVAILABILITY"), ("subcontractor", "SUBCONTRACT_AGREEMENT"),
    ("driver", "DRIVER_VEHICLE"),
]


def target_list(path: Path, opts: ImportOptions) -> ImportPlan:
    plan = ImportPlan("target_list")
    for rec in read_table(path, "Targets", header_contains=["Tier", "Organisation"]):
        plan.rows_read += 1
        company = col(rec, "Organisation")
        if not company:
            plan.rows_skipped += 1
            continue
        sector = str(col(rec, "Sector") or "").lower()
        where = str(col(rec, "Where to apply") or "")
        notes = col(rec, "Notes")
        is_pm = "plan manager" in company.lower() or "plan manager" in str(notes or "").lower()
        if "council" in sector or sector.startswith("health"):
            category, ptype, otype = "GOVERNMENT", "PANEL_SUPPLIER", "TENDER"
        elif "injury" in sector:
            category, ptype, otype = "INSURER", "PANEL_SUPPLIER", "TENDER"
        elif is_pm:
            category, ptype, otype = "PLAN_MANAGER", "MARKETPLACE", "OTHER"
        else:
            category = "AGED_CARE_PROVIDER" if sector.startswith("aged care") else "PROVIDER"
            ptype, otype = "ASSOCIATE_PROVIDER", "ASSOCIATE_PROVIDER"
        tier = as_int(col(rec, "Tier"))
        org = _org_ref(company)
        phone = col(rec, "Phone")
        plan.add("organisation", **org, org_category=category,
                 phone=phone if phone and re.search(r"\d", str(phone)) else None)
        plan.add("opportunity", organisation=org, opportunity_type_code=otype, match_any_type=True,
                 source_key=f"target_list:{name_key(company)}",
                 source_code="GOVERNMENT_TENDER" if category in ("GOVERNMENT", "INSURER") else "PROVIDER_WEBSITE",
                 source_detail="GCDC target list (23 Aug 2026)", priority={1: "P1", 2: "P2"}.get(tier, "P3"),
                 priority_mode="raise_only", discovered_at="2026-08-23", notes=notes)
        low = where.lower()
        method = ("PHONE" if "phone" in low and "." not in low.split("-")[0] else "PORTAL" if "login" in low
                  else "ONLINE_FORM" if "." in where else "OTHER")
        url = where.split(" ")[0] if "." in where.split(" ")[0] else None
        status = _APP_STATUS.get(str(col(rec, "Status") or "").strip().lower(), "NOT_STARTED")
        plan.add("application", organisation=org, partnership_type=ptype, application_method=method,
                 application_url=url, status=status, priority_tier=tier, notes=notes,
                 next_action=None if status != "NOT_STARTED" else (
                     f"Phone {company} and ask for the supplier / associate provider process" if method == "PHONE"
                     else f"Complete the application at {url}" if url else None),
                 source_key=f"target_list:{name_key(company)}")
    try:
        for rec in read_table(path, "Compliance pack", header_contains=["Document", "Have it?"]):
            doc = str(col(rec, "Document") or "").lower()
            code = next((c for k, c in _DOC_KEYWORDS if k in doc), None)
            if not code:
                continue
            expiry, _ = safe_date(col(rec, "Expires"))
            plan.add("compliance_document", doc_code=code, is_held=yes(col(rec, "Have it?")) or 0,
                     expiry_date=expiry.isoformat() if expiry else None, notes=col(rec, "Notes"))
    except ValueError:
        pass
    return plan


# ---------------------------------------------------------------------------
# 7. crm-master.csv — 52 scored leads from the commercial opportunity audit
# ---------------------------------------------------------------------------
_TYPE_KEYWORDS = [
    ("subcontract", "SUBCONTRACTING"), ("associate", "ASSOCIATE_PROVIDER"), ("support at home", "SUPPORT_AT_HOME"),
    ("aged", "AGED_CARE"), ("support coordination", "SUPPORT_COORDINATION"), ("referral", "SUPPORT_COORDINATION"),
    ("allied", "ALLIED_HEALTH_REFERRAL"), ("occupational", "ALLIED_HEALTH_REFERRAL"), ("sil", "SIL"),
    ("hospital", "HOSPITAL_DISCHARGE"), ("tender", "TENDER"), ("workforce", "WORKFORCE"), ("direct", "PARTICIPANT_DIRECT"),
]
_SOURCE_KEYWORDS = [
    ("indeed", "INDEED"), ("seek", "SEEK"), ("facebook", "FACEBOOK"), ("carevicinity", "CAREVICINITY"),
    ("ndis", "NDIS_REGISTER"), ("niisq", "NIISQ_REGISTER"), ("tender", "GOVERNMENT_TENDER"),
    ("council", "GOVERNMENT_TENDER"), ("website", "PROVIDER_WEBSITE"), ("web research", "GOOGLE"),
    ("google", "GOOGLE"), ("referral", "REFERRAL"), ("linkedin", "LINKEDIN"),
]
_CATEGORY_KEYWORDS = [
    ("support coordinator", "SUPPORT_COORDINATOR"), ("plan manag", "PLAN_MANAGER"), ("allied", "ALLIED_HEALTH"),
    ("occupational", "ALLIED_HEALTH"), ("aged", "AGED_CARE_PROVIDER"), ("platform", "MARKETPLACE"),
    ("council", "GOVERNMENT"), ("hospital", "HOSPITAL"), ("provider", "PROVIDER"),
]


def _keyword(text: Any, table: list[tuple[str, str]], default: str | None) -> str | None:
    low = str(text or "").lower()
    return next((code for k, code in table if k in low), default)


def crm_master(path: Path, opts: ImportOptions) -> ImportPlan:
    plan = ImportPlan("crm_master")
    for rec in read_table(path, None, header_contains=["Lead ID", "Company"]):
        plan.rows_read += 1
        company, lead_id = col(rec, "Company"), col(rec, "Lead ID")
        if not company or not lead_id:
            plan.rows_skipped += 1
            continue
        website = col(rec, "Website")
        org = _org_ref(company, None, website if website and "." in str(website) else None)
        plan.add("organisation", **org, phone=col(rec, "Phone"), region=col(rec, "Location"),
                 org_category=_keyword(col(rec, "Lead Type"), _CATEGORY_KEYWORDS, None))
        email = normalise_email(col(rec, "Email"))
        name = clean(col(rec, "Contact Name"))
        if email or name:
            plan.add("contact", organisation=org, email=email, full_name=name, role_title=col(rec, "Title"),
                     is_decision_maker=1 if name else None,
                     email_provenance="ORG_WEBSITE" if str(col(rec, "Verified Email?") or "").upper().startswith("VERIFIED")
                     else "UNKNOWN")
        prio = {"IMMEDIATE": "P1", "HIGH": "P2"}.get(str(col(rec, "Priority") or "").strip().upper(), "P3")
        nxt, problem = safe_date(col(rec, "Next Follow-Up"), not_before=opts.records_start)
        if problem:
            plan.problems.append(f"crm_master row {rec['_row']}: {problem}")
        source_text = col(rec, "Source")
        opp = {"source_key": str(lead_id),
               "opportunity_type_code": _keyword(f"{col(rec, 'Opportunity Type')} {col(rec, 'Lead Type')}",
                                                 _TYPE_KEYWORDS, "OTHER")}
        plan.add("opportunity", organisation=org, **opp,
                 source_code=_keyword(source_text, _SOURCE_KEYWORDS, "OTHER"), source_detail=source_text,
                 priority=prio, priority_mode="raise_only", score=col(rec, "Score"),
                 next_action=col(rec, "Next Action"), next_action_due=nxt.isoformat() if nxt else None,
                 discovered_at="2026-09-03", notes=col(rec, "Notes"))
        first, _ = safe_date(col(rec, "First Contact Date"), not_before=opts.records_start)
        if first:
            _touch(plan, opts, org=org, opp=opp, email=email, when=first,
                   step=1, campaign="AUDIT_LEADS", subject=None, source_key=f"crm_master:{lead_id}:1",
                   row=rec["_row"], channel="EMAIL" if email else "PHONE")
    return plan


# ---------------------------------------------------------------------------
# 8. GCDC Facebook Intent Radar — discovery sheet (worked examples are skipped)
# ---------------------------------------------------------------------------
_FB_TYPES = {"a": None, "b": "PARTICIPANT_DIRECT", "c": "OTHER", "d": "SUBCONTRACTING"}


def facebook_radar(path: Path, opts: ImportOptions) -> ImportPlan:
    plan = ImportPlan("facebook_radar")
    for rec in read_table(path, None, header_contains=["Date Found", "Lead Type"]):
        plan.rows_read += 1
        notes = str(col(rec, "Notes") or "")
        if re.search(r"worked example|synthetic", notes, re.IGNORECASE):
            plan.rows_skipped += 1
            continue
        lead = str(col(rec, "Lead Type") or "").strip().lower()
        otype = _FB_TYPES.get(lead[:1], "OTHER")
        if otype is None:  # job seekers are recruitment leads, not revenue opportunities
            plan.rows_skipped += 1
            continue
        found, problem = safe_date(col(rec, "Date Found"), not_before=opts.records_start)
        if problem or not found:
            plan.problems.append(f"facebook_radar row {rec['_row']}: {problem or 'no Date Found'}")
            continue
        company = col(rec, "Organisation") or f"Facebook — {col(rec, 'Group') or 'group post'}"
        org = _org_ref(company)
        plan.add("organisation", **org, region=col(rec, "Location"),
                 org_category="OTHER" if not col(rec, "Organisation") else None)
        score = as_int(col(rec, "Intent Score")) or 0
        post_url = col(rec, "Post URL")
        plan.add("opportunity", organisation=org, opportunity_type_code=otype, source_code="FACEBOOK",
                 source_url=post_url if post_url and str(post_url).startswith("http") else None,
                 source_detail=f"Facebook group: {col(rec, 'Group')}", source_key=str(post_url or f"{company}:{found}"),
                 title=f"{col(rec, 'Need') or 'Support need'} — {col(rec, 'Location') or 'location unknown'}",
                 expected_weekly_hours=col(rec, "Estimated Hours"), rate_basis=None,
                 priority="P1" if score >= 80 else "P2" if score >= 60 else "P3", score=score,
                 next_action=col(rec, "Next Action"), next_action_due=col(rec, "Next Action Date"),
                 owner=col(rec, "Owner") or "Ritik", discovered_at=found.isoformat(), notes=notes or None)
    return plan


IMPORTERS: dict[str, Callable[[Path, ImportOptions], ImportPlan]] = {
    "campaign-lists": campaign_lists,
    "sah-provider-db": sah_provider_db,
    "crm-master": crm_master,
    "target-list": target_list,
    "referral-crm": referral_crm,
    "coordinator-emails": coordinator_emails,
    "outreach-queue": outreach_queue,
    "facebook-radar": facebook_radar,
}

# `gcdc import all` order: most precise sources first, so organisation
# categories and evidence levels come from the best-researched sheet.
IMPORT_ORDER = list(IMPORTERS)
