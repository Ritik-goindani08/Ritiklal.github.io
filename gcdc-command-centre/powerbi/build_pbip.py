#!/usr/bin/env python3
"""Generate the GCDC Command Centre Power BI Project (PBIP).

    python powerbi/build_pbip.py                    # SharePoint / OneDrive source (default)
    python powerbi/build_pbip.py --source local     # local export folder (Power BI Desktop only)

Output: powerbi/GCDC Command Centre.pbip + .SemanticModel (TMDL) + .Report (PBIR).

Everything is derived from the gcdc export registry, so the model always
matches the CSVs `gcdc export` writes. Re-run after any change to the views.
Business logic lives in SQL; DAX here only aggregates precomputed columns.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from gcdc.db import connect, migrate  # noqa: E402
from gcdc.export import export_schema  # noqa: E402

NAME = "GCDC Command Centre"
NS = uuid.UUID("6f1c8a52-2b7e-4d3a-9d6e-5c0de6c0dcc0")

SCHEMAS = {
    "pbip": "https://developer.microsoft.com/json-schemas/fabric/pbip/pbipProperties/1.0.0/schema.json",
    "pbism": "https://developer.microsoft.com/json-schemas/fabric/item/semanticModel/definitionProperties/1.0.0/schema.json",
    "pbir": "https://developer.microsoft.com/json-schemas/fabric/item/report/definitionProperties/2.0.0/schema.json",
    "report": "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/report/3.0.0/schema.json",
    "version": "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/versionMetadata/1.0.0/schema.json",
    "pages": "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/pagesMetadata/1.0.0/schema.json",
    "page": "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/page/2.0.0/schema.json",
    "visual": "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/visualContainer/2.4.0/schema.json",
    "platform": "https://developer.microsoft.com/json-schemas/fabric/gitIntegration/platformProperties/2.0.0/schema.json",
}


def gid(*parts: str) -> str:
    return str(uuid.uuid5(NS, "/".join(parts)))


def hid(*parts: str) -> str:
    return uuid.uuid5(NS, "/".join(parts)).hex[:20]


# =============================================================================
# Semantic model
# =============================================================================
TABLE_NAMES = {
    "pbi_dim_date": "Dim Date", "pbi_dim_region": "Dim Region", "pbi_dim_stage": "Dim Stage",
    "pbi_dim_opportunity_type": "Dim Opportunity Type", "pbi_dim_source": "Dim Source",
    "pbi_dim_service_type": "Dim Service Type", "pbi_dim_partnership_stage": "Dim Partnership Stage",
    "pbi_dim_organisation": "Dim Organisation", "pbi_dim_worker": "Dim Worker", "pbi_dim_participant": "Dim Participant",
    "pbi_dim_agent": "Dim Agent", "pbi_dim_dq_check": "Dim DQ Check",
    "pbi_action_centre": "Action Centre", "pbi_kpi_current": "KPI Current", "pbi_funnel": "Funnel Summary",
    "pbi_meta": "Meta", "pbi_targets": "Targets", "pbi_daily_brief": "Daily Brief", "pbi_daily_focus": "Daily Focus",
}


def table_name(export: str) -> str:
    if export in TABLE_NAMES:
        return TABLE_NAMES[export]
    base = export.removeprefix("pbi_").removeprefix("fact_")
    return "Fact " + " ".join(w.upper() if w in ("dq",) else w.capitalize() for w in base.split("_"))


# (from table, from column, to table, to column, active)
RELATIONSHIPS: list[tuple[str, str, str, str, bool]] = []


def _rel(ft, fc, tt, tc, active=True):
    RELATIONSHIPS.append((ft, fc, tt, tc, active))


for fact in ("Fact Opportunity", "Fact Outreach", "Fact Reply", "Fact Meeting", "Fact Follow Up", "Fact Referral",
             "Fact Trial Shift", "Fact Roster", "Fact Revenue", "Fact Partnership", "Fact Application", "Fact Contact",
             "Action Centre"):
    _rel(fact, "organisation_id", "Dim Organisation", "organisation_id")
for fact in ("Fact Opportunity", "Fact Outreach", "Fact Reply", "Fact Meeting", "Fact Follow Up", "Fact Referral",
             "Fact Trial Shift", "Fact Roster", "Fact Revenue", "Fact Partnership", "Fact Application", "Fact Contact",
             "Fact Worker Availability", "Fact Worker Capacity", "Fact Worker Requirement", "Action Centre"):
    _rel(fact, "region_code", "Dim Region", "region_code")
for fact in ("Fact Opportunity", "Fact Roster", "Fact Revenue"):
    _rel(fact, "opportunity_type_code", "Dim Opportunity Type", "opportunity_type_code")
    _rel(fact, "source_code", "Dim Source", "source_code")
for fact in ("Fact Roster", "Fact Revenue", "Fact Referral", "Fact Worker Requirement"):
    _rel(fact, "service_type_code", "Dim Service Type", "service_type_code")
for fact in ("Fact Worker Availability", "Fact Worker Capacity", "Fact Roster", "Fact Trial Shift"):
    _rel(fact, "worker_id", "Dim Worker", "worker_id")
for fact in ("Fact Roster", "Fact Revenue"):
    _rel(fact, "participant_id", "Dim Participant", "participant_id")
_rel("Fact Agent Run", "agent_code", "Dim Agent", "agent_code")
_rel("Fact Agent Health", "agent_code", "Dim Agent", "agent_code")
_rel("Fact Data Quality", "check_code", "Dim DQ Check", "check_code")
for fact, col in (("Fact Opportunity", "discovered_date"), ("Fact Outreach", "activity_date"),
                  ("Fact Reply", "received_date"), ("Fact Meeting", "meeting_date"), ("Fact Referral", "received_date"),
                  ("Fact Revenue", "service_date"), ("Fact Daily Snapshot", "snapshot_date"),
                  ("Fact Trial Shift", "trial_date"), ("Fact Agent Run", "run_date"), ("Fact Follow Up", "due_date")):
    _rel(fact, col, "Dim Date", "date")
_rel("Fact Revenue", "paid_date", "Dim Date", "date", active=False)

SORT_BY = {
    ("Dim Region", "region_name"): "sort_order", ("Dim Stage", "stage_name"): "stage_order",
    ("Dim Opportunity Type", "opportunity_type_name"): "sort_order", ("Dim Source", "source_group"): "source_group_order",
    ("Dim Service Type", "service_type_name"): "sort_order", ("Dim Partnership Stage", "partnership_stage_name"): "stage_order",
    ("Dim Date", "month_name"): "month_number", ("Dim Date", "day_name"): "day_of_week",
    ("Fact Partnership", "partnership_stage_name"): "partnership_stage_order",
    ("Funnel Summary", "stage_name"): "stage_order", ("Fact Agent Health", "rag_label"): "rag_sort",
}

MONEY, MONEY2, COUNT, HOURS, PCT, DATE, NUM = "$#,0", "$#,0.00", "#,0", "#,0.0", "0.0%", "dd mmm yyyy", "#,0.##"


@dataclass
class Measure:
    name: str
    expr: str
    fmt: str | None = None
    folder: str = ""
    description: str = ""


def _asof(body: str) -> str:
    return f"VAR d = [As Of Date]\nRETURN\n    {body}"


def _week(body: str) -> str:
    return f"VAR d = [As Of Date]\nVAR ws = d - WEEKDAY(d, 2) + 1\nRETURN\n    {body}"


def _month(body: str) -> str:
    return f"VAR d = [As Of Date]\nVAR ms = DATE(YEAR(d), MONTH(d), 1)\nRETURN\n    {body}"


def _kpi(col: str) -> str:
    return f"MAX('KPI Current'[{col}])"


FO, FR, FOUT = "'Fact Opportunity'", "'Fact Roster'", "'Fact Outreach'"
MEASURES: list[Measure] = [
    # ---- meta ---------------------------------------------------------------------------------------
    Measure("As Of Date", "MAX('Meta'[as_of_date])", DATE, "0 Meta", "Date the export was taken (Brisbane)."),
    Measure("Data As At", "\"Data as at \" & FORMAT(MAX('Meta'[as_of_local]), \"d mmm yyyy h:nn AM/PM\")", None, "0 Meta"),
    # ---- revenue: ACTUAL ----------------------------------------------------------------------------
    Measure("MRR", f"SUM({FR}[mrr_contribution])", MONEY, "1 Revenue (actual)",
            "Monthly recurring revenue: active rosters x approved rate x weeks per month."),
    Measure("Projected MRR", f"SUM({FR}[projected_mrr_contribution])", MONEY, "1 Revenue (actual)",
            "Active plus confirmed scheduled rosters with approved rates."),
    Measure("Revenue Target", _kpi("revenue_target"), MONEY, "1 Revenue (actual)"),
    Measure("Revenue Gap", "MAX(0, [Revenue Target] - [MRR])", MONEY, "1 Revenue (actual)"),
    Measure("MRR % of Target", "DIVIDE([MRR], [Revenue Target])", PCT, "1 Revenue (actual)"),
    Measure("Gauge Max (MRR)", "MAX([Revenue Target], [MRR]) * 1.2", MONEY, "1 Revenue (actual)"),
    Measure("Weekly Billable Hours", f"SUM({FR}[active_weekly_hours])", HOURS, "1 Revenue (actual)"),
    Measure("Weekly Hours Target (Min)", _kpi("weekly_hours_target_min"), HOURS, "1 Revenue (actual)"),
    Measure("Weekly Hours Target (Max)", _kpi("weekly_hours_target_max"), HOURS, "1 Revenue (actual)"),
    Measure("Weekly Hours Gap", "MAX(0, [Weekly Hours Target (Min)] - [Weekly Billable Hours])", HOURS,
            "1 Revenue (actual)"),
    Measure("Gauge Max (Hours)", "MAX([Weekly Hours Target (Max)], [Weekly Billable Hours]) * 1.2", HOURS,
            "1 Revenue (actual)"),
    Measure("Avg Billable Rate",
            f"DIVIDE(SUM({FR}[active_weekly_value_with_rate]), SUM({FR}[active_hours_with_rate]))", MONEY2,
            "1 Revenue (actual)"),
    Measure("Avg Billable Rate (display)",
            "IF(ISBLANK([Avg Billable Rate]), \"UNKNOWN\", FORMAT([Avg Billable Rate], \"$#,0.00\") & \"/hr\")", None,
            "1 Revenue (actual)"),
    Measure("Active Rosters", f"CALCULATE(COUNTROWS({FR}), {FR}[is_active] = 1)", COUNT, "1 Revenue (actual)"),
    Measure("Active Participants",
            f"CALCULATE(DISTINCTCOUNT({FR}[participant_id]), {FR}[is_active] = 1, NOT ISBLANK({FR}[participant_id]))",
            COUNT, "1 Revenue (actual)"),
    Measure("Participants / Rosters",
            "FORMAT([Active Participants] + 0, \"0\") & \" / \" & FORMAT([Active Rosters] + 0, \"0\")", None,
            "1 Revenue (actual)"),
    Measure("Rosters Without Approved Rate", f"CALCULATE(COUNTROWS({FR}), {FR}[active_rate_not_approved] = 1)", COUNT,
            "1 Revenue (actual)", "Active rosters excluded from MRR because the rate is unknown or not approved."),
    Measure("Actual Revenue", "SUM('Fact Revenue'[recognised_amount])", MONEY, "1 Revenue (actual)",
            "Invoiced revenue (ex GST, excluding void/written-off), by service date."),
    Measure("Revenue Received",
            "CALCULATE(SUM('Fact Revenue'[amount_received]), USERELATIONSHIP('Fact Revenue'[paid_date], 'Dim Date'[date]))",
            MONEY, "1 Revenue (actual)"),
    Measure("Revenue Received MTD", _month("CALCULATE(SUM('Fact Revenue'[amount_received]), REMOVEFILTERS('Dim Date'), "
                                           "'Fact Revenue'[paid_date] >= ms, 'Fact Revenue'[paid_date] <= d)"),
            MONEY, "1 Revenue (actual)"),
    Measure("Revenue Invoiced MTD", _month("CALCULATE(SUM('Fact Revenue'[recognised_amount]), REMOVEFILTERS('Dim Date'), "
                                           "'Fact Revenue'[service_date] >= ms, 'Fact Revenue'[service_date] <= d)"),
            MONEY, "1 Revenue (actual)"),
    Measure("Snapshot MRR", "SUM('Fact Daily Snapshot'[mrr])", MONEY, "1 Revenue (actual)"),
    Measure("Snapshot Revenue Target", "SUM('Fact Daily Snapshot'[revenue_target])", MONEY, "1 Revenue (actual)"),
    Measure("Snapshot Weekly Hours", "SUM('Fact Daily Snapshot'[weekly_billable_hours])", HOURS, "1 Revenue (actual)"),
    # ---- pipeline: ESTIMATES (never added to actuals) ---------------------------------------------------
    Measure("Active Opportunities", f"CALCULATE(COUNTROWS({FO}), {FO}[is_active] = 1)", COUNT, "2 Pipeline (estimated)"),
    Measure("P1 Opportunities", f"CALCULATE([Active Opportunities], {FO}[priority] = \"P1\")", COUNT, "2 Pipeline (estimated)"),
    Measure("P2 Opportunities", f"CALCULATE([Active Opportunities], {FO}[priority] = \"P2\")", COUNT, "2 Pipeline (estimated)"),
    Measure("P3 Opportunities", f"CALCULATE([Active Opportunities], {FO}[priority] = \"P3\")", COUNT, "2 Pipeline (estimated)"),
    Measure("Pipeline Value (known)", f"CALCULATE(SUM({FO}[est_monthly_value]), {FO}[is_active] = 1)", MONEY,
            "2 Pipeline (estimated)", "Estimated monthly value of open opportunities where hours AND rate are known."),
    Measure("Pipeline Value Unknown Count",
            f"CALCULATE(COUNTROWS({FO}), {FO}[is_active] = 1, {FO}[value_status] <> \"KNOWN\")", COUNT,
            "2 Pipeline (estimated)"),
    Measure("Near-Term Pipeline (known)", f"CALCULATE(SUM({FO}[est_monthly_value]), {FO}[pipeline_horizon] = \"NEAR_TERM\")",
            MONEY, "2 Pipeline (estimated)"),
    Measure("Near-Term Unknown Count",
            f"CALCULATE(COUNTROWS({FO}), {FO}[pipeline_horizon] = \"NEAR_TERM\", {FO}[value_status] <> \"KNOWN\")", COUNT,
            "2 Pipeline (estimated)"),
    Measure("Longer-Term Pipeline (known)",
            f"CALCULATE(SUM({FO}[est_monthly_value]), {FO}[pipeline_horizon] = \"LONGER_TERM\")", MONEY,
            "2 Pipeline (estimated)"),
    Measure("Longer-Term Unknown Count",
            f"CALCULATE(COUNTROWS({FO}), {FO}[pipeline_horizon] = \"LONGER_TERM\", {FO}[value_status] <> \"KNOWN\")",
            COUNT, "2 Pipeline (estimated)"),
    Measure("Near-Term Pipeline (display)",
            "FORMAT([Near-Term Pipeline (known)] + 0, \"$#,0\") & \"/mo known + \" & "
            "FORMAT([Near-Term Unknown Count] + 0, \"0\") & \" UNKNOWN\"", None, "2 Pipeline (estimated)"),
    Measure("Longer-Term Pipeline (display)",
            "FORMAT([Longer-Term Pipeline (known)] + 0, \"$#,0\") & \"/mo known + \" & "
            "FORMAT([Longer-Term Unknown Count] + 0, \"0\") & \" UNKNOWN\"", None, "2 Pipeline (estimated)"),
    Measure("Weighted Pipeline (stage rules)", f"CALCULATE(SUM({FO}[weighted_monthly_value]), {FO}[is_active] = 1)",
            MONEY, "2 Pipeline (estimated)", "Known value x stage probability from config/gcdc.toml business rules."),
    Measure("Potential Weekly Hours", f"CALCULATE(SUM({FO}[expected_weekly_hours]), {FO}[is_active] = 1)", HOURS,
            "2 Pipeline (estimated)"),
    Measure("Opportunities Found", f"COUNTROWS({FO})", COUNT, "3 Discovery"),
    Measure("Opportunities Found Today",
            _asof(f"CALCULATE(COUNTROWS({FO}), REMOVEFILTERS('Dim Date'), {FO}[discovered_date] = d)"), COUNT, "3 Discovery"),
    Measure("Opportunities Found This Week",
            _week(f"CALCULATE(COUNTROWS({FO}), REMOVEFILTERS('Dim Date'), {FO}[discovered_date] >= ws, "
                  f"{FO}[discovered_date] <= d)"), COUNT, "3 Discovery"),
    Measure("Opportunities Found This Month",
            _month(f"CALCULATE(COUNTROWS({FO}), REMOVEFILTERS('Dim Date'), {FO}[discovered_date] >= ms, "
                   f"{FO}[discovered_date] <= d)"), COUNT, "3 Discovery"),
    Measure("New Organisations (month)",
            _month(f"CALCULATE(DISTINCTCOUNT({FO}[organisation_id]), REMOVEFILTERS('Dim Date'), {FO}[discovered_date] >= ms, "
                   f"{FO}[discovered_date] <= d, {FO}[is_new_organisation] = 1)"), COUNT, "3 Discovery"),
    Measure("Existing Orgs With New Opportunities (month)",
            _month(f"CALCULATE(DISTINCTCOUNT({FO}[organisation_id]), REMOVEFILTERS('Dim Date'), {FO}[discovered_date] >= ms, "
                   f"{FO}[discovered_date] <= d, {FO}[is_new_organisation] = 0)"), COUNT, "3 Discovery"),
    # ---- funnel ---------------------------------------------------------------------------------------
    Measure("Funnel Count",
            f"VAR s = SELECTEDVALUE('Dim Stage'[stage_order])\nRETURN\n    IF(ISBLANK(s), COUNTROWS({FO}), "
            f"CALCULATE(COUNTROWS({FO}), {FO}[stage_order_reached] >= s))", COUNT, "4 Funnel"),
    Measure("Funnel Conversion %",
            f"VAR s = SELECTEDVALUE('Dim Stage'[stage_order])\n"
            f"VAR cur = CALCULATE(COUNTROWS({FO}), {FO}[stage_order_reached] >= s)\n"
            f"VAR prev = CALCULATE(COUNTROWS({FO}), {FO}[stage_order_reached] >= s - 1)\n"
            f"RETURN\n    IF(ISBLANK(s) || s = 1, BLANK(), DIVIDE(cur, prev))", PCT, "4 Funnel",
            "Share of opportunities at the previous stage that reached this stage."),
    Measure("Funnel % of Found", f"DIVIDE([Funnel Count], COUNTROWS({FO}))", PCT, "4 Funnel"),
    Measure("Partnership Funnel Count",
            "VAR s = SELECTEDVALUE('Dim Partnership Stage'[stage_order])\nRETURN\n    IF(ISBLANK(s), "
            "COUNTROWS('Fact Partnership'), CALCULATE(COUNTROWS('Fact Partnership'), "
            "'Fact Partnership'[partnership_stage_order] >= s, 'Fact Partnership'[status] = \"ACTIVE\"))", COUNT, "4 Funnel"),
    Measure("Positive Conversations (30d)", _kpi("positive_conversations_30d"), COUNT, "4 Funnel"),
    Measure("Meetings Booked (30d)", _kpi("meetings_booked_30d"), COUNT, "4 Funnel"),
    Measure("Participant Referrals (30d)", _kpi("referrals_30d"), COUNT, "4 Funnel"),
    Measure("Trial Shifts (30d)", _kpi("trial_shifts_30d"), COUNT, "4 Funnel"),
    # ---- outreach ---------------------------------------------------------------------------------------
    Measure("Emails Sent", f"SUM({FOUT}[is_sent])", COUNT, "5 Outreach"),
    Measure("Emails Attempted", f"SUM({FOUT}[is_attempted])", COUNT, "5 Outreach"),
    Measure("First-Touch Sent", f"SUM({FOUT}[is_first_touch_sent])", COUNT, "5 Outreach"),
    Measure("Follow-Ups Sent", f"SUM({FOUT}[is_follow_up_sent])", COUNT, "5 Outreach"),
    Measure("First-Touch Sent Today",
            _asof(f"CALCULATE(SUM({FOUT}[is_first_touch_sent]), REMOVEFILTERS('Dim Date'), {FOUT}[sent_date] = d)"),
            COUNT, "5 Outreach"),
    Measure("First-Touch Sent This Week",
            _week(f"CALCULATE(SUM({FOUT}[is_first_touch_sent]), REMOVEFILTERS('Dim Date'), {FOUT}[sent_date] >= ws, "
                  f"{FOUT}[sent_date] <= d)"), COUNT, "5 Outreach"),
    Measure("First-Touch Sent This Month",
            _month(f"CALCULATE(SUM({FOUT}[is_first_touch_sent]), REMOVEFILTERS('Dim Date'), {FOUT}[sent_date] >= ms, "
                   f"{FOUT}[sent_date] <= d)"), COUNT, "5 Outreach"),
    Measure("Follow-Ups Sent This Month",
            _month(f"CALCULATE(SUM({FOUT}[is_follow_up_sent]), REMOVEFILTERS('Dim Date'), {FOUT}[sent_date] >= ms, "
                   f"{FOUT}[sent_date] <= d)"), COUNT, "5 Outreach"),
    Measure("Emails Scheduled (pending)", f"CALCULATE(SUM({FOUT}[is_scheduled]), REMOVEFILTERS('Dim Date'))", COUNT,
            "5 Outreach"),
    Measure("Cancelled Emails", f"SUM({FOUT}[is_cancelled])", COUNT, "5 Outreach"),
    Measure("Bounced Emails", f"SUM({FOUT}[is_bounced])", COUNT, "5 Outreach"),
    Measure("Bounce Rate", "DIVIDE([Bounced Emails], [Emails Attempted])", PCT, "5 Outreach"),
    Measure("Contacts", "COUNTROWS('Fact Contact')", COUNT, "5 Outreach"),
    Measure("Verified Contacts", "SUM('Fact Contact'[is_verified_email])", COUNT, "5 Outreach"),
    Measure("Invalid Contacts", "SUM('Fact Contact'[is_invalid_email])", COUNT, "5 Outreach"),
    Measure("Orgs Contacted", f"CALCULATE(DISTINCTCOUNT({FOUT}[organisation_id]), {FOUT}[is_sent] = 1)", COUNT, "5 Outreach"),
    Measure("Orgs Contacted (all time)", "CALCULATE([Orgs Contacted], REMOVEFILTERS('Dim Date'))", COUNT, "5 Outreach"),
    Measure("Orgs Replied",
            "CALCULATE(DISTINCTCOUNT('Fact Reply'[organisation_id]), 'Fact Reply'[is_human_reply] = 1)", COUNT, "5 Outreach"),
    Measure("Orgs Positive",
            "CALCULATE(DISTINCTCOUNT('Fact Reply'[organisation_id]), 'Fact Reply'[is_positive] = 1)", COUNT, "5 Outreach"),
    Measure("Orgs With Meeting",
            "CALCULATE(DISTINCTCOUNT('Fact Meeting'[organisation_id]), 'Fact Meeting'[is_booked_or_held] = 1)", COUNT,
            "5 Outreach"),
    Measure("Orgs With Referral", "DISTINCTCOUNT('Fact Referral'[organisation_id])", COUNT, "5 Outreach"),
    Measure("Orgs With Trial Shift",
            "CALCULATE(DISTINCTCOUNT('Fact Trial Shift'[organisation_id]), 'Fact Trial Shift'[is_scheduled_or_done] = 1)",
            COUNT, "5 Outreach"),
    Measure("Orgs With Roster", f"CALCULATE(DISTINCTCOUNT({FR}[organisation_id]), {FR}[status] <> \"\")", COUNT,
            "5 Outreach"),
    Measure("Replies", "SUM('Fact Reply'[is_human_reply])", COUNT, "5 Outreach"),
    Measure("Positive Replies", "SUM('Fact Reply'[is_positive])", COUNT, "5 Outreach"),
    Measure("Reply Rate", "CALCULATE(DIVIDE([Orgs Replied], [Orgs Contacted]), REMOVEFILTERS('Dim Date'))", PCT, "5 Outreach",
            "Organisations that replied / organisations contacted."),
    Measure("Positive Reply Rate", "CALCULATE(DIVIDE([Orgs Positive], [Orgs Contacted]), REMOVEFILTERS('Dim Date'))", PCT,
            "5 Outreach"),
    Measure("Meeting Conversion", "CALCULATE(DIVIDE([Orgs With Meeting], [Orgs Contacted]), REMOVEFILTERS('Dim Date'))",
            PCT, "5 Outreach"),
    Measure("Referral Conversion", "CALCULATE(DIVIDE([Orgs With Referral], [Orgs Contacted]), REMOVEFILTERS('Dim Date'))",
            PCT, "5 Outreach"),
    Measure("Trial-Shift Conversion",
            "CALCULATE(DIVIDE([Orgs With Trial Shift], [Orgs Contacted]), REMOVEFILTERS('Dim Date'))", PCT, "5 Outreach"),
    Measure("Roster Conversion", "CALCULATE(DIVIDE([Orgs With Roster], [Orgs Contacted]), REMOVEFILTERS('Dim Date'))",
            PCT, "5 Outreach"),
    Measure("Hours per 100 Orgs Contacted",
            f"DIVIDE(SUM({FR}[active_hours_from_contacted_orgs]), [Orgs Contacted (all time)]) * 100", HOURS, "5 Outreach",
            "Recurring weekly billable hours now active from contacted organisations, per 100 contacted."),
    Measure("Revenue per 100 Orgs Contacted",
            "DIVIDE(CALCULATE(SUM('Fact Revenue'[revenue_from_contacted_orgs]), REMOVEFILTERS('Dim Date')), "
            "[Orgs Contacted (all time)]) * 100", MONEY, "5 Outreach",
            "Actual revenue from contacted organisations, per 100 contacted."),
    # ---- today / action centre (company-wide, same numbers as the daily brief) --------------------------
    Measure("Emails Scheduled Today", _kpi("emails_scheduled_today"), COUNT, "6 Today"),
    Measure("Follow-Ups Due Today", _kpi("followups_due_today"), COUNT, "6 Today"),
    Measure("Overdue Follow-Ups", _kpi("followups_overdue"), COUNT, "6 Today"),
    Measure("Replies Awaiting Response", _kpi("replies_awaiting_response"), COUNT, "6 Today"),
    Measure("Items Needing Approval", _kpi("approvals_pending"), COUNT, "6 Today"),
    Measure("Meetings Today", _kpi("meetings_today"), COUNT, "6 Today"),
    Measure("Meetings Upcoming (7d)", _kpi("meetings_upcoming"), COUNT, "6 Today"),
    Measure("Referrals Needing Action", _kpi("referrals_needing_action"), COUNT, "6 Today"),
    Measure("Applications To Complete", _kpi("applications_to_complete"), COUNT, "6 Today"),
    Measure("Bounced Emails (recent)", _kpi("bounces_recent"), COUNT, "6 Today"),
    Measure("Failed Agents", _kpi("agents_red"), COUNT, "6 Today"),
    Measure("Action Items", "COUNTROWS('Action Centre')", COUNT, "6 Today"),
    Measure("Overdue Actions", "CALCULATE(COUNTROWS('Action Centre'), 'Action Centre'[due_status] = \"OVERDUE\")", COUNT,
            "6 Today"),
    # ---- partnerships -----------------------------------------------------------------------------------
    Measure("Active Partnerships", "CALCULATE(COUNTROWS('Fact Partnership'), 'Fact Partnership'[is_active] = 1)", COUNT,
            "7 Partnerships"),
    Measure("Approved Partners",
            "CALCULATE(COUNTROWS('Fact Partnership'), 'Fact Partnership'[partnership_stage_order] >= 6, "
            "'Fact Partnership'[is_active] = 1)", COUNT, "7 Partnerships"),
    Measure("Documents Outstanding", "SUM('Fact Partnership'[documents_outstanding])", COUNT, "7 Partnerships"),
    Measure("Partner Hours Received", "SUM('Fact Partnership'[hours_received])", HOURS, "7 Partnerships"),
    Measure("Partner Revenue", "SUM('Fact Partnership'[revenue_generated])", MONEY, "7 Partnerships"),
    Measure("Applications In Progress",
            "CALCULATE(COUNTROWS('Fact Application'), 'Fact Application'[needs_completion] = 1)", COUNT, "7 Partnerships"),
    Measure("Applications Submitted",
            "CALCULATE(COUNTROWS('Fact Application'), 'Fact Application'[status] IN {\"SUBMITTED\", \"UNDER_REVIEW\"})",
            COUNT, "7 Partnerships"),
    Measure("Compliance Docs Current", "SUM('Fact Compliance Document'[is_current])", COUNT, "7 Partnerships"),
    Measure("Compliance Pack",
            "FORMAT([Compliance Docs Current] + 0, \"0\") & \" / \" & FORMAT(COUNTROWS('Fact Compliance Document') + 0, \"0\")",
            None, "7 Partnerships"),
    # ---- workforce --------------------------------------------------------------------------------------
    Measure("Active Workers (verified)",
            "CALCULATE(COUNTROWS('Fact Worker Capacity'), 'Fact Worker Capacity'[is_verified_active] = 1)", COUNT,
            "8 Workforce"),
    Measure("Available Workers", "SUM('Fact Worker Capacity'[is_available])", COUNT, "8 Workforce"),
    Measure("Capacity Hours", "SUM('Fact Worker Capacity'[capacity_weekly_hours])", HOURS, "8 Workforce"),
    Measure("Allocated Hours",
            "CALCULATE(SUM('Fact Worker Capacity'[allocated_weekly_hours]), 'Fact Worker Capacity'[is_verified_active] = 1)",
            HOURS, "8 Workforce"),
    Measure("Available Hours", "SUM('Fact Worker Capacity'[available_weekly_hours])", HOURS, "8 Workforce"),
    Measure("Utilisation", "DIVIDE([Allocated Hours], [Capacity Hours])", PCT, "8 Workforce"),
    Measure("Open Worker Requirements",
            "CALCULATE(COUNTROWS('Fact Worker Requirement'), 'Fact Worker Requirement'[is_open] = 1)", COUNT, "8 Workforce"),
    Measure("Unfilled Rosters", f"CALCULATE(COUNTROWS({FR}), {FR}[is_unfilled] = 1)", COUNT, "8 Workforce"),
    Measure("Weekday Capacity Hours",
            "CALCULATE(SUM('Fact Worker Availability'[capacity_hours]), 'Fact Worker Availability'[is_weekday] = 1)",
            HOURS, "8 Workforce"),
    Measure("Weekend Capacity Hours",
            "CALCULATE(SUM('Fact Worker Availability'[capacity_hours]), 'Fact Worker Availability'[is_weekend] = 1)",
            HOURS, "8 Workforce"),
    Measure("Evening Capacity Hours",
            "CALCULATE(SUM('Fact Worker Availability'[capacity_hours]), 'Fact Worker Availability'[is_evening] = 1)",
            HOURS, "8 Workforce"),
    Measure("Overnight Capacity Hours",
            "CALCULATE(SUM('Fact Worker Availability'[capacity_hours]), 'Fact Worker Availability'[is_overnight] = 1)",
            HOURS, "8 Workforce"),
    Measure("Workers: Personal Care",
            "CALCULATE([Active Workers (verified)], 'Fact Worker Capacity'[cap_personal_care] = 1)", COUNT, "8 Workforce"),
    Measure("Workers: Community Access",
            "CALCULATE([Active Workers (verified)], 'Fact Worker Capacity'[cap_community_access] = 1)", COUNT, "8 Workforce"),
    Measure("Workers: Complex Support",
            "CALCULATE([Active Workers (verified)], 'Fact Worker Capacity'[cap_complex_support] = 1)", COUNT, "8 Workforce"),
    Measure("Workers: Domestic",
            "CALCULATE([Active Workers (verified)], 'Fact Worker Capacity'[cap_domestic] = 1)", COUNT, "8 Workforce"),
    Measure("Referrals", "COUNTROWS('Fact Referral')", COUNT, "9 Regional"),
    # ---- agents & data quality --------------------------------------------------------------------------
    Measure("Agents Green", "CALCULATE(COUNTROWS('Fact Agent Health'), 'Fact Agent Health'[rag_status] = \"GREEN\")",
            COUNT, "10 Agent Health"),
    Measure("Agents Amber", "CALCULATE(COUNTROWS('Fact Agent Health'), 'Fact Agent Health'[rag_status] = \"AMBER\")",
            COUNT, "10 Agent Health"),
    Measure("Agents Red", "CALCULATE(COUNTROWS('Fact Agent Health'), 'Fact Agent Health'[rag_status] = \"RED\")", COUNT,
            "10 Agent Health"),
    Measure("Agent Runs", "COUNTROWS('Fact Agent Run')", COUNT, "10 Agent Health"),
    Measure("Failed Runs (7d)",
            _asof("CALCULATE(COUNTROWS('Fact Agent Run'), REMOVEFILTERS('Dim Date'), 'Fact Agent Run'[status] = \"FAILED\", "
                  "'Fact Agent Run'[run_date] >= d - 6)"), COUNT, "10 Agent Health"),
    Measure("Open Alerts", "CALCULATE(COUNTROWS('Fact Alert'), 'Fact Alert'[status] <> \"RESOLVED\")", COUNT,
            "10 Agent Health"),
    Measure("DQ Errors", "CALCULATE(COUNTROWS('Fact Data Quality'), 'Fact Data Quality'[severity] = \"ERROR\")", COUNT,
            "10 Agent Health"),
    Measure("DQ Warnings", "CALCULATE(COUNTROWS('Fact Data Quality'), 'Fact Data Quality'[severity] = \"WARNING\")",
            COUNT, "10 Agent Health"),
]

M_TYPES = {"int64": "Int64.Type", "double": "type number", "string": "type text", "date": "type date",
           "dateTime": "type datetime"}
TMDL_TYPES = {"int64": "int64", "double": "double", "string": "string", "date": "dateTime", "dateTime": "dateTime"}


def _fmt_for(col: str, ctype: str) -> str | None:
    if ctype == "date":
        return DATE
    if ctype == "dateTime":
        return "dd mmm yyyy hh:nn"
    if ctype == "int64":
        return "0" if col.endswith(("_id", "_order", "_sort")) or col in ("tier", "year", "iso_week") else COUNT
    if ctype == "double":
        if re.search(r"(probability|utilisation|pct|rate$|conversion)", col) and "hourly" not in col:
            return PCT
        if re.search(r"(value|amount|revenue|mrr|target|gap|received|invoiced|hourly_rate|avg_billable)", col):
            return MONEY
        return NUM
    return None


def _hidden(table: str, col: str) -> bool:
    return (col.endswith("_id") and not table.startswith("Dim")) or col in (
        "action_key", "entity_type", "entity_id", "priority_rank", "rag_sort", "severity_sort", "priority_sort",
        "source_group_order", "month_number") or col.endswith("_sort")


def tq(name: str) -> str:
    """TMDL object name, quoted only when needed (TOM matches refs to declarations textually)."""
    return name if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) else "'" + name.replace("'", "''") + "'"


def _dax_block(expr: str, indent: str) -> str:
    lines = expr.split("\n")
    if len(lines) == 1:
        return f" {lines[0]}"
    return "\n" + "\n".join(indent + line for line in lines)


def _desc(text: str, indent: str) -> str:
    return "".join(f"{indent}/// {line}\n" for line in text.split("\n")) if text else ""


SITE_PLACEHOLDER = "https://YOUR-TENANT-my.sharepoint.com/personal/YOUR_ACCOUNT/"


def build_model(out: Path, schema: dict[str, dict[str, str]], source: str, site_url: str = "",
                folder_path: str = "Documents/GCDC Command Centre/powerbi",
                local_folder: str = "C:\\GCDC\\gcdc-command-centre\\exports\\powerbi") -> None:
    defn = out / "definition"
    tables_dir = defn / "tables"
    tables_dir.mkdir(parents=True)
    (out / "definition.pbism").write_text(json.dumps(
        {"$schema": SCHEMAS["pbism"], "version": "4.2", "settings": {}}, indent=2), encoding="utf-8")
    (out / ".platform").write_text(json.dumps(
        {"$schema": SCHEMAS["platform"], "metadata": {"type": "SemanticModel", "displayName": NAME},
         "config": {"version": "2.0", "logicalId": gid("semanticmodel")}}, indent=2), encoding="utf-8")
    (defn / "database.tmdl").write_text("database\n\tcompatibilityLevel: 1600\n\n", encoding="utf-8")

    names = [table_name(t) for t in schema] + ["_Measures"]
    params = (["ExportSiteUrl", "ExportFolderPath"] if source == "sharepoint" else ["ExportLocalFolder"])
    model = ["model Model", "\tculture: en-AU", "\tdefaultPowerBIDataSourceVersion: powerBI_V3",
             "\tdiscourageImplicitMeasures", "\tsourceQueryCulture: en-AU", "\tdataAccessOptions",
             "\t\tlegacyRedirects", "\t\treturnErrorValuesAsNull", "",
             f"annotation PBI_QueryOrder = {json.dumps(params + ['GcdcFolder', 'GcdcTable'] + names[:-1])}", "",
             "annotation __PBI_TimeIntelligenceEnabled = 0", ""]
    model += [f"ref table {tq(n)}" for n in names]
    (defn / "model.tmdl").write_text("\n".join(model) + "\n", encoding="utf-8")

    # ---- shared expressions: parameters, the export folder, and the CSV loader ----
    if source == "sharepoint":
        folder = ("let\n    Root = SharePoint.Contents(ExportSiteUrl, [ApiVersion = 15]),\n"
                  "    Parts = List.Select(Text.Split(ExportFolderPath, \"/\"), each _ <> \"\"),\n"
                  "    Folder = List.Accumulate(Parts, Root, (state, part) => state{[Name = part]}[Content])\n"
                  "in\n    Folder")
        param_defs = [
            ("ExportSiteUrl", site_url or SITE_PLACEHOLDER,
             "OneDrive for Business (or SharePoint site) URL that holds the export folder."),
            ("ExportFolderPath", folder_path, "Library/folder path of the gcdc export inside that site."),
        ]
    else:
        folder = "let\n    Folder = Folder.Contents(ExportLocalFolder)\nin\n    Folder"
        param_defs = [("ExportLocalFolder", local_folder, "Local folder written by `gcdc export`.")]
    loader = ("(tableName as text) as table =>\nlet\n    Content = GcdcFolder{[Name = tableName & \".csv\"]}[Content],\n"
              "    Csv = Csv.Document(Content, [Delimiter = \",\", Encoding = 65001, QuoteStyle = QuoteStyle.Csv]),\n"
              "    Promoted = Table.PromoteHeaders(Csv, [PromoteAllScalars = true]),\n"
              "    Nulls = Table.TransformColumns(Promoted, {}, each if _ = \"\" then null else _)\n"
              "in\n    Nulls")
    exprs = []
    for pname, value, desc in param_defs:
        exprs.append(f"/// {desc}\nexpression {pname} = {json.dumps(value)} meta [IsParameterQuery = true, "
                     f"Type = \"Text\", IsParameterQueryRequired = true]\n\tlineageTag: {gid('expr', pname)}\n\n"
                     f"\tannotation PBI_ResultType = Text\n")
    exprs.append("/// The export folder (navigation table of CSV files).\nexpression GcdcFolder =" +
                 _dax_block(folder, "\t\t") + f"\n\tlineageTag: {gid('expr', 'GcdcFolder')}\n\n"
                 "\tannotation PBI_ResultType = Table\n")
    exprs.append("/// Loads one exported CSV by table name; blank cells become null.\nexpression GcdcTable =" +
                 _dax_block(loader, "\t\t") + f"\n\tlineageTag: {gid('expr', 'GcdcTable')}\n\n"
                 "\tannotation PBI_ResultType = Function\n")
    (defn / "expressions.tmdl").write_text("\n".join(exprs), encoding="utf-8")

    # ---- tables ----
    for export, cols in schema.items():
        tname = table_name(export)
        q = tq(tname)
        lines = [f"table {q}", f"\tlineageTag: {gid('table', tname)}"]
        if tname == "Dim Date":
            lines.append("\tdataCategory: Time")
        lines.append("")
        for col, ctype in cols.items():
            lines.append(f"\tcolumn {tq(col)}")
            lines.append(f"\t\tdataType: {TMDL_TYPES[ctype]}")
            if tname == "Dim Date" and col == "date":
                lines.append("\t\tisKey")
            if _hidden(tname, col):
                lines.append("\t\tisHidden")
            fmt = _fmt_for(col, ctype)
            if fmt:
                lines.append(f"\t\tformatString: {fmt}")
            lines.append(f"\t\tlineageTag: {gid('col', tname, col)}")
            lines.append("\t\tsummarizeBy: none")
            lines.append(f"\t\tsourceColumn: {col}")
            if (tname, col) in SORT_BY:
                lines.append(f"\t\tsortByColumn: {SORT_BY[(tname, col)]}")
            lines.append("")
            lines.append("\t\tannotation SummarizationSetBy = Automatic")
            if ctype == "date":
                lines.append("")
                lines.append("\t\tannotation UnderlyingDateTimeDataType = Date")
            lines.append("")
        typed = ",\n".join(f"        {{\"{c}\", {M_TYPES[t]}}}" for c, t in cols.items())
        mexpr = (f"let\n    Source = GcdcTable(\"{export}\"),\n    Typed = Table.TransformColumnTypes(Source, {{\n{typed}\n"
                 f"    }}, \"en-AU\")\nin\n    Typed")
        lines.append(f"\tpartition {q} = m")
        lines.append("\t\tmode: import")
        lines.append("\t\tsource =" + _dax_block(mexpr, "\t\t\t\t"))
        lines.append("")
        lines.append("\tannotation PBI_ResultType = Table")
        lines.append("")
        (tables_dir / f"{tname}.tmdl").write_text("\n".join(lines), encoding="utf-8")

    # ---- measure table ----
    ml = ["table _Measures", f"\tlineageTag: {gid('table', '_Measures')}", ""]
    for m in MEASURES:
        if m.description:
            ml.append(_desc(m.description, "\t").rstrip("\n"))
        ml.append(f"\tmeasure {tq(m.name)} =" + _dax_block(m.expr, "\t\t\t"))
        if m.fmt:
            ml.append(f"\t\tformatString: {m.fmt}")
        if m.folder:
            ml.append(f"\t\tdisplayFolder: {m.folder}")
        ml.append(f"\t\tlineageTag: {gid('measure', m.name)}")
        ml.append("")
    ml += ["\tcolumn 'Measure Table'", "\t\tdataType: string", "\t\tisHidden", f"\t\tlineageTag: {gid('col', '_Measures')}",
           "\t\tsummarizeBy: none", "\t\tsourceColumn: Measure Table", "", "\t\tannotation SummarizationSetBy = Automatic", "",
           "\tpartition _Measures = m", "\t\tmode: import", "\t\tsource =",
           "\t\t\t\tlet", "\t\t\t\t    Source = #table(type table [#\"Measure Table\" = text], {})", "\t\t\t\tin",
           "\t\t\t\t    Source", "", "\tannotation PBI_ResultType = Table", ""]
    (tables_dir / "_Measures.tmdl").write_text("\n".join(ml), encoding="utf-8")

    # ---- relationships ----
    rl = []
    for ft, fc, tt, tc, active in RELATIONSHIPS:
        rl.append(f"relationship {gid('rel', ft, fc, tt, tc)}")
        if not active:
            rl.append("\tisActive: false")
        rl.append(f"\tfromColumn: {tq(ft)}.{tq(fc)}")
        rl.append(f"\ttoColumn: {tq(tt)}.{tq(tc)}")
        rl.append("")
    (defn / "relationships.tmdl").write_text("\n".join(rl), encoding="utf-8")


# =============================================================================
# Report (PBIR)
# =============================================================================
@dataclass(frozen=True)
class F:
    table: str
    name: str
    kind: str = "column"  # column | measure
    label: str | None = None


def M(name: str, label: str | None = None) -> F:
    return F("_Measures", name, "measure", label)


def C(table: str, name: str, label: str | None = None) -> F:
    return F(table, name, "column", label)


def _field(f: F) -> dict:
    key = "Measure" if f.kind == "measure" else "Column"
    return {key: {"Expression": {"SourceRef": {"Entity": f.table}}, "Property": f.name}}


def _proj(f: F) -> dict:
    p = {"field": _field(f), "queryRef": f"{f.table}.{f.name}", "nativeQueryRef": f.label or f.name}
    if f.label:
        p["displayName"] = f.label
    return p


def _lit(value: str) -> dict:
    return {"expr": {"Literal": {"Value": value}}}


@dataclass
class Visual:
    vtype: str
    x: int
    y: int
    w: int
    h: int
    roles: dict[str, list[F]] = field(default_factory=dict)
    title: str | None = None
    sort: tuple[F, str] | None = None
    filters: list[tuple[F, list[str]]] = field(default_factory=list)
    text: str | None = None
    text_size: str = "14pt"


@dataclass
class Page:
    key: str
    name: str
    visuals: list[Visual]
    height: int = 720
    width: int = 1280


def card(m: F, x, y, w, h=84, title=None) -> Visual:
    return Visual("card", x, y, w, h, {"Values": [m]}, title or m.label or m.name)


def cards(ms: list[F], y: int, h: int = 84, x0: int = 16, total: int = 1248, gap: int = 8) -> list[Visual]:
    w = (total - gap * (len(ms) - 1)) // len(ms)
    return [card(m, x0 + i * (w + gap), y, w, h) for i, m in enumerate(ms)]


def text(t: str, x, y, w, h=40, size="18pt") -> Visual:
    return Visual("textbox", x, y, w, h, text=t, text_size=size)


def table(fields: list[F], x, y, w, h, title, sort=None, filters=None) -> Visual:
    return Visual("tableEx", x, y, w, h, {"Values": fields}, title, sort, filters or [])


def chart(vtype: str, category: F | None, values: list[F], x, y, w, h, title, series: F | None = None,
          sort=None, filters=None) -> Visual:
    roles = {"Y": values}
    if category:
        roles["Category"] = [category]
    if series:
        roles["Series"] = [series]
    return Visual(vtype, x, y, w, h, roles, title, sort, filters or [])


def header(title: str) -> list[Visual]:
    return [text(title, 16, 8, 900, 44, "20pt"), card(M("Data As At"), 924, 8, 340, 44, title=" ")]


def _pages() -> list[Page]:
    DO, DR, DD = "Dim Organisation", "Dim Region", "Dim Date"
    pages = []
    # 1 -------------------------------------------------------------------------------------------------
    pages.append(Page("p01_executive", "Executive", header("GCDC Command Centre — Executive") + cards(
        [M("MRR", "Monthly Recurring Revenue"), M("Revenue Target"), M("Revenue Gap"),
         M("Weekly Billable Hours", "Recurring Weekly Hours"), M("Active Opportunities"), M("P1 Opportunities")], 60) + cards(
        [M("Positive Conversations (30d)"), M("Meetings Booked (30d)"), M("Participant Referrals (30d)"),
         M("Trial Shifts (30d)"), M("Participants / Rosters", "Active Participants / Rosters")], 152) + [
        Visual("gauge", 16, 244, 400, 226, {"Y": [M("MRR")], "MinValue": [], "MaxValue": [M("Gauge Max (MRR)")],
                                             "TargetValue": [M("Revenue Target")]}, "Revenue progress to target (MRR)"),
        Visual("gauge", 16, 478, 400, 226, {"Y": [M("Weekly Billable Hours")], "MaxValue": [M("Gauge Max (Hours)")],
                                             "TargetValue": [M("Weekly Hours Target (Min)")]},
               "Recurring weekly hours vs target"),
        chart("funnel", C("Dim Stage", "stage_name", "Stage"), [M("Funnel Count", "Opportunities")], 424, 244, 480, 460,
              "Sales funnel — opportunity to recurring roster"),
        table([C("Dim Stage", "stage_name", "Stage"), M("Funnel Count", "Reached"),
               M("Funnel Conversion %", "Conversion from previous"), M("Funnel % of Found", "% of found")],
              912, 244, 352, 460, "Stage-to-stage conversion", sort=(C("Dim Stage", "stage_name"), "Ascending")),
    ]))
    # 2 -------------------------------------------------------------------------------------------------
    pages.append(Page("p02_today", "Today — Action Centre", header("Today — what needs attention") + cards(
        [M("Emails Scheduled Today"), M("Follow-Ups Due Today"), M("Overdue Follow-Ups"), M("Replies Awaiting Response"),
         M("Items Needing Approval", "Needs Ritik's approval")], 60, 80) + cards(
        [M("Referrals Needing Action"), M("Meetings Today"), M("Applications To Complete", "Applications / forms to complete"),
         M("Bounced Emails (recent)", "Bounced emails (7 days)"), M("Failed Agents", "Failed / silent agents")], 148, 80) + [
        table([C("Daily Focus", "focus_rank", "#"), C("Daily Focus", "focus_title", "Focus"),
               C("Daily Focus", "focus_why", "Why it moves recurring revenue"), C("Daily Focus", "brief_date", "Brief")],
              16, 236, 1248, 118, "Today's focus — Daily BI Agent", sort=(C("Daily Focus", "focus_rank"), "Ascending")),
        table([C("Action Centre", "priority", "Priority"), C("Action Centre", "category", "Type"),
               C("Action Centre", "organisation_name", "Organisation"),
               C("Action Centre", "opportunity_title", "Opportunity"), C("Action Centre", "current_stage", "Current stage"),
               C("Action Centre", "next_action", "Next action"), C("Action Centre", "owner", "Owner"),
               C("Action Centre", "due_date", "Due"), C("Action Centre", "status_label", "Status"),
               C("Action Centre", "potential_revenue_display", "Potential revenue"),
               C("Action Centre", "last_activity_date", "Last activity"), C("Action Centre", "action_score", "Rank")],
              16, 362, 1248, 342, "Priority actions (highest rank first)",
              sort=(C("Action Centre", "action_score"), "Descending")),
    ]))
    # 3 -------------------------------------------------------------------------------------------------
    pages.append(Page("p03_revenue", "Revenue", header("Revenue — actual (never mixed with pipeline)") + [
        text("ACTUAL REVENUE", 16, 56, 400, 28, "12pt")] + cards(
        [M("MRR", "Monthly Recurring Revenue"), M("Projected MRR", "Projected MRR (incl. confirmed starts)"),
         M("Revenue Received MTD"), M("Revenue Invoiced MTD"), M("Weekly Billable Hours"),
         M("Avg Billable Rate (display)", "Average billable rate"),
         M("Rosters Without Approved Rate", "Rosters excluded (rate unknown)")], 86, 84) + [
        chart("clusteredColumnChart", C(DD, "year_month", "Month"),
              [M("Actual Revenue", "Invoiced (actual)"), M("Revenue Received", "Received (actual)")],
              16, 178, 612, 260, "Actual revenue by month"),
        chart("lineChart", C(DD, "date", "Date"), [M("Snapshot MRR", "MRR"), M("Snapshot Revenue Target", "Target")],
              636, 178, 628, 260, "MRR vs target (daily snapshots)"),
        chart("clusteredBarChart", C("Dim Participant", "participant_ref", "Participant"), [M("Actual Revenue")],
              16, 446, 300, 258, "Revenue by participant", sort=(M("Actual Revenue"), "Descending")),
        chart("clusteredBarChart", C(DO, "organisation_name", "Organisation / partner"), [M("Actual Revenue")],
              324, 446, 300, 258, "Revenue by organisation / partner", sort=(M("Actual Revenue"), "Descending")),
        chart("clusteredBarChart", C(DR, "region_name", "Region"), [M("Actual Revenue"), M("MRR")],
              632, 446, 300, 258, "Revenue by region"),
        chart("clusteredBarChart", C("Dim Service Type", "service_type_name", "Service type"), [M("Actual Revenue")],
              940, 446, 324, 148, "Revenue by service type"),
        Visual("multiRowCard", 940, 600, 324, 104, {"Values": [
            M("Near-Term Pipeline (display)", "Qualified near-term"), M("Longer-Term Pipeline (display)", "Longer-term")]},
            "ESTIMATED PIPELINE VALUE — not revenue"),
    ]))
    # 4 -------------------------------------------------------------------------------------------------
    pages.append(Page("p04_outreach", "Outreach Performance", header("Outreach performance — conversion over volume") + cards(
        [M("First-Touch Sent Today"), M("First-Touch Sent This Week"), M("First-Touch Sent This Month"),
         M("Follow-Ups Sent This Month"), M("Emails Scheduled (pending)", "Scheduled emails"), M("Cancelled Emails"),
         M("Bounced Emails"), M("Verified Contacts"), M("Invalid Contacts", "Invalid contacts found")], 60, 76) + cards(
        [M("Reply Rate"), M("Positive Reply Rate"), M("Meeting Conversion"), M("Referral Conversion"),
         M("Trial-Shift Conversion"), M("Roster Conversion")], 144, 76, total=820) + [
        card(M("Hours per 100 Orgs Contacted", "Recurring hours per 100 orgs contacted"), 844, 144, 206, 76),
        card(M("Revenue per 100 Orgs Contacted", "Revenue per 100 orgs contacted"), 1058, 144, 206, 76),
        chart("clusteredColumnChart", C(DD, "week_start", "Week"), [M("First-Touch Sent"), M("Follow-Ups Sent"),
                                                                   M("Bounced Emails")],
              16, 228, 740, 476, "Outreach volume by week"),
        table([C("Fact Outreach", "campaign_code", "Campaign"), M("Emails Sent"), M("Orgs Contacted"),
               M("Bounced Emails"), M("Bounce Rate")], 764, 228, 500, 476, "By campaign",
              sort=(M("Emails Sent"), "Descending")),
    ]))
    # 5 -------------------------------------------------------------------------------------------------
    pages.append(Page("p05_discovery", "Opportunity Discovery", header("Opportunity discovery") + cards(
        [M("Opportunities Found Today", "Found today"), M("Opportunities Found This Week", "Found this week"),
         M("Opportunities Found This Month", "Found this month"), M("New Organisations (month)", "New organisations (month)"),
         M("Existing Orgs With New Opportunities (month)", "Existing orgs, new opps (month)"),
         M("P1 Opportunities", "P1 (open)"), M("P2 Opportunities", "P2 (open)"), M("P3 Opportunities", "P3 (open)")], 60) + [
        chart("clusteredBarChart", C("Dim Source", "source_group", "Source"), [M("Opportunities Found")],
              16, 152, 400, 270, "Opportunities by source"),
        chart("clusteredBarChart", C("Dim Opportunity Type", "opportunity_type_name", "Type"), [M("Opportunities Found")],
              424, 152, 400, 270, "Opportunities by type"),
        chart("clusteredColumnChart", C(DD, "year_month", "Month"), [M("Actual Revenue")], 832, 152, 432, 270,
              "Actual revenue by source over time", series=C("Dim Source", "source_group", "Source")),
        table([C("Fact Opportunity", "discovered_date", "Found"), C(DO, "organisation_name", "Organisation"),
               C("Fact Opportunity", "title", "Opportunity"), C("Dim Opportunity Type", "opportunity_type_name", "Type"),
               C("Dim Source", "source_group", "Source"), C("Fact Opportunity", "priority", "Priority"),
               C("Fact Opportunity", "stage_name", "Stage"), C("Fact Opportunity", "est_monthly_value_display", "Est. $/mo")],
              16, 430, 1248, 274, "Latest opportunities", sort=(C("Fact Opportunity", "discovered_date"), "Descending")),
    ]))
    # 6 -------------------------------------------------------------------------------------------------
    FP = "Fact Partnership"
    pages.append(Page("p06_partnerships", "Partnership Pipeline", header("Partnership pipeline") + cards(
        [M("Active Partnerships"), M("Approved Partners"), M("Applications In Progress", "Applications to complete"),
         M("Applications Submitted"), M("Documents Outstanding"), M("Compliance Pack", "Compliance pack current"),
         M("Partner Hours Received"), M("Partner Revenue")], 60) + [
        chart("funnel", C("Dim Partnership Stage", "partnership_stage_name", "Stage"),
              [M("Partnership Funnel Count", "Partnerships")], 16, 152, 360, 552, "Lead → recurring revenue"),
        table([C(DO, "organisation_name", "Company"), C(FP, "partnership_type", "Type"),
               C(FP, "partnership_stage_name", "Stage"), C(FP, "application_status", "Application"),
               C(FP, "onboarding_status", "Onboarding"), C(FP, "documents_required", "Docs required"),
               C(FP, "documents_outstanding", "Docs outstanding"), C(FP, "documents_outstanding_list", "Outstanding"),
               C(FP, "insurance_requirements", "Insurance"), C(FP, "labour_hire_licence_required", "Labour-hire licence"),
               C(FP, "ndis_registration_required", "NDIS registration"),
               C(FP, "last_communication_date", "Last communication"), C(FP, "next_action", "Next action"),
               C(FP, "approval_date", "Approved"), C(FP, "first_work_received_date", "First work"),
               C(FP, "hours_received", "Hours received"), C(FP, "revenue_generated", "Revenue")],
              384, 152, 880, 360, "Partnerships", sort=(C(FP, "partnership_stage_name"), "Descending")),
        table([C("Fact Compliance Document", "doc_name", "Document"), C("Fact Compliance Document", "status_label", "Status"),
               C("Fact Compliance Document", "expiry_date", "Expires"),
               C("Fact Compliance Document", "partnerships_waiting", "Partners waiting")],
              384, 520, 880, 184, "GCDC compliance pack", sort=(C("Fact Compliance Document", "status_label"), "Descending")),
    ]))
    # 7 -------------------------------------------------------------------------------------------------
    FOP = "Fact Opportunity"
    pages.append(Page("p07_money", "Money on the Table", header("Money on the table") + cards(
        [M("MRR", "Current MRR (actual)"), M("Near-Term Pipeline (display)", "Qualified near-term pipeline"),
         M("Longer-Term Pipeline (display)", "Longer-term pipeline"), M("Potential Weekly Hours"),
         M("Weighted Pipeline (stage rules)", "Weighted (stage rules only)")], 60) + [
        text("Estimated values use known hours × known rate only. Blank values are UNKNOWN (see Value status) — "
             "nothing is assumed, and estimates are never added to actual revenue.", 16, 150, 1248, 32, "10pt"),
        table([C(DO, "organisation_name", "Organisation"), C(FOP, "participant_ref", "Participant ref"),
               C("Dim Opportunity Type", "opportunity_type_name", "Type"), C(FOP, "expected_weekly_hours", "Hours/wk"),
               C(FOP, "expected_hourly_rate", "Rate"), C(FOP, "rate_basis", "Rate basis"),
               C(FOP, "est_weekly_value", "Est. $/wk"), C(FOP, "est_monthly_value", "Est. $/mo"),
               C(FOP, "value_status", "Value status"), C(FOP, "stage_name", "Stage"),
               C(FOP, "stage_probability", "Stage probability (rule)"), C(FOP, "pipeline_horizon", "Horizon"),
               C(FOP, "priority", "Priority"), C(FOP, "next_action", "Next action"), C(FOP, "age_days", "Age (days)")],
              16, 188, 1248, 516, "Open opportunities", sort=(C(FOP, "est_monthly_value"), "Descending"),
              filters=[(C(FOP, "is_active"), ["1L"])]),
    ]))
    # 8 -------------------------------------------------------------------------------------------------
    FWC = "Fact Worker Capacity"
    pages.append(Page("p08_workforce", "Worker Capacity", header("Worker capacity — verified workers only") + cards(
        [M("Active Workers (verified)"), M("Available Workers"), M("Capacity Hours", "Weekly capacity hours"),
         M("Allocated Hours", "Allocated weekly hours"), M("Available Hours", "Available weekly hours"), M("Utilisation"),
         M("Open Worker Requirements"), M("Unfilled Rosters", "Unfilled shifts / rosters")], 60) + [
        chart("clusteredBarChart", C(DR, "region_name", "Region"), [M("Capacity Hours"), M("Allocated Hours")],
              16, 152, 400, 270, "Capacity by region"),
        chart("clusteredBarChart", None, [M("Weekday Capacity Hours", "Weekdays"), M("Weekend Capacity Hours", "Weekends"),
                                          M("Evening Capacity Hours", "Evenings"), M("Overnight Capacity Hours", "Overnight")],
              424, 152, 400, 270, "Capacity by time band (hours/week)"),
        chart("clusteredBarChart", None, [M("Workers: Personal Care", "Personal care"),
                                          M("Workers: Community Access", "Community access"),
                                          M("Workers: Complex Support", "Complex support"), M("Workers: Domestic", "Domestic")],
              832, 152, 432, 130, "Verified workers by capability"),
        chart("clusteredBarChart", C(FWC, "gender_for_matching", "Gender (matching only)"), [M("Capacity Hours")],
              832, 290, 432, 132, "Capacity by gender (for participant matching)"),
        table([C("Dim Worker", "worker_ref", "Worker"), C("Dim Worker", "display_name", "Name"),
               C("Dim Worker", "region_code", "Home region"), C("Dim Worker", "is_verified_active", "Verified"),
               C("Dim Worker", "verification_gaps", "Missing checks"), M("Capacity Hours"), M("Allocated Hours"),
               M("Available Hours"), M("Utilisation")], 16, 430, 760, 274, "Workers"),
        table([C("Fact Worker Requirement", "region_code", "Region"),
               C("Fact Worker Requirement", "required_capability", "Capability"),
               C("Fact Worker Requirement", "gender_requirement", "Gender req."),
               C("Fact Worker Requirement", "weekly_hours", "Hours/wk"), C("Fact Worker Requirement", "needed_by", "Needed by"),
               C("Fact Worker Requirement", "status", "Status")], 784, 430, 480, 274, "Open worker requirements",
              filters=[(C("Fact Worker Requirement", "is_open"), ["1L"])]),
    ]))
    # 9 -------------------------------------------------------------------------------------------------
    pages.append(Page("p09_regional", "Regional Performance", header("Regional performance") + [
        table([C(DR, "region_name", "Region"), M("Active Opportunities", "Opportunities"),
               M("Orgs Contacted (all time)", "Orgs contacted"), M("Orgs Replied", "Replies (orgs)"), M("Referrals"),
               M("Active Participants", "Participants"), M("Weekly Billable Hours", "Weekly hours"),
               M("Actual Revenue", "Revenue"), M("Capacity Hours", "Worker capacity (h/wk)")],
              16, 60, 1248, 300, "Results by region", sort=(C(DR, "region_name"), "Ascending")),
        chart("clusteredBarChart", C(DR, "region_name", "Region"), [M("Weekly Billable Hours")], 16, 368, 304, 336,
              "Weekly hours"),
        chart("clusteredBarChart", C(DR, "region_name", "Region"), [M("Actual Revenue")], 328, 368, 304, 336, "Revenue"),
        chart("clusteredBarChart", C(DR, "region_name", "Region"), [M("Active Opportunities")], 640, 368, 304, 336,
              "Open opportunities"),
        chart("clusteredBarChart", C(DR, "region_name", "Region"), [M("Orgs Contacted (all time)"), M("Orgs Replied")],
              952, 368, 312, 336, "Contacted vs replied"),
    ]))
    # 10 ------------------------------------------------------------------------------------------------
    FAH = "Fact Agent Health"
    pages.append(Page("p10_agents", "Agent Health", header("Agent health & data quality") + cards(
        [M("Agents Green", "GREEN — operating normally"), M("Agents Amber", "AMBER — warning / incomplete"),
         M("Agents Red", "RED — intervention required"), M("Failed Runs (7d)"), M("Open Alerts"), M("DQ Errors"),
         M("DQ Warnings")], 60) + [
        table([C(FAH, "agent_name", "Agent"), C(FAH, "rag_label", "Status"),
               C(FAH, "last_successful_run", "Last success"), C(FAH, "last_attempted_run", "Last attempt"),
               C(FAH, "last_run_status", "Last result"), C(FAH, "last_runtime_seconds", "Runtime (s)"),
               C(FAH, "records_processed", "Processed"), C(FAH, "records_created", "Created"),
               C(FAH, "errors_count", "Errors"), C(FAH, "last_error", "Last error"),
               C(FAH, "next_scheduled_run", "Next run"), C(FAH, "schedule_description", "Schedule"),
               C(FAH, "rag_reason", "Why")], 16, 152, 1248, 292, "Automations", sort=(C(FAH, "rag_label"), "Ascending")),
        table([C("Dim DQ Check", "check_name", "Check"), C("Fact Data Quality", "severity", "Severity"),
               C("Fact Data Quality", "entity_type", "Record type"), C("Fact Data Quality", "entity_id", "Record id"),
               C("Fact Data Quality", "detail", "Detail")], 16, 452, 760, 252, "Data-quality issues (latest run)",
              sort=(C("Fact Data Quality", "severity"), "Ascending")),
        chart("clusteredColumnChart", C(DD, "date", "Date"), [M("Agent Runs")], 784, 452, 480, 252,
              "Agent runs by day", series=C("Fact Agent Run", "status", "Result")),
    ]))
    return pages


THEME = {
    "name": "GCDC Command Centre",
    "dataColors": ["#1B365D", "#0F7B8A", "#E6A117", "#2E8540", "#C0392B", "#6C5B7B", "#7F8C8D", "#48A9A6", "#F28F3B",
                   "#4B6584"],
    "background": "#FFFFFF", "foreground": "#1B365D", "tableAccent": "#0F7B8A",
    "good": "#2E8540", "neutral": "#E6A117", "bad": "#C0392B", "maximum": "#1B365D", "center": "#E6A117",
    "minimum": "#C0392B",
}


def _visual_json(page_key: str, i: int, v: Visual) -> dict:
    name = hid(page_key, str(i), v.vtype)
    body: dict = {"visualType": v.vtype, "drillFilterOtherVisuals": True}
    if v.vtype == "textbox":
        body["objects"] = {"general": [{"properties": {"paragraphs": [{"textRuns": [
            {"value": v.text, "textStyle": {"fontWeight": "bold" if v.text_size != "10pt" else "normal",
                                           "fontSize": v.text_size, "color": "#1B365D"}}]}]}}]}
    else:
        qs = {role: {"projections": [_proj(f) for f in fields]} for role, fields in v.roles.items() if fields}
        query: dict = {"queryState": qs}
        if v.sort:
            f, direction = v.sort
            query["sortDefinition"] = {"sort": [{"field": _field(f), "direction": direction}], "isDefaultSort": False}
        body["query"] = query
        if v.title is not None:
            body["visualContainerObjects"] = {"title": [{"properties": {
                "show": _lit("true"), "text": _lit("'" + v.title.replace("'", "''") + "'")}}]}
    out = {"$schema": SCHEMAS["visual"], "name": name,
           "position": {"x": v.x, "y": v.y, "z": i * 1000, "height": v.h, "width": v.w, "tabOrder": i * 1000},
           "visual": body}
    if v.filters:
        filters = []
        for n, (f, values) in enumerate(v.filters):
            filters.append({
                "name": hid(page_key, str(i), "filter", str(n)), "field": _field(f), "type": "Categorical",
                "filter": {"Version": 2, "From": [{"Name": "t", "Entity": f.table, "Type": 0}],
                           "Where": [{"Condition": {"In": {
                               "Expressions": [{"Column": {"Expression": {"SourceRef": {"Source": "t"}},
                                                           "Property": f.name}}],
                               "Values": [[{"Literal": {"Value": val}}] for val in values]}}}]},
                "howCreated": "User"})
        out["filterConfig"] = {"filters": filters}
    return out


def build_report(out: Path, semantic_folder: str) -> list[Page]:
    defn = out / "definition"
    (defn / "pages").mkdir(parents=True)
    res = out / "StaticResources" / "RegisteredResources"
    res.mkdir(parents=True)
    (res / "GCDC_Theme.json").write_text(json.dumps(THEME, indent=2), encoding="utf-8")
    (out / "definition.pbir").write_text(json.dumps(
        {"$schema": SCHEMAS["pbir"], "version": "4.0", "datasetReference": {"byPath": {"path": f"../{semantic_folder}"}}},
        indent=2), encoding="utf-8")
    (out / ".platform").write_text(json.dumps(
        {"$schema": SCHEMAS["platform"], "metadata": {"type": "Report", "displayName": NAME},
         "config": {"version": "2.0", "logicalId": gid("report")}}, indent=2), encoding="utf-8")
    (defn / "version.json").write_text(json.dumps({"$schema": SCHEMAS["version"], "version": "2.0.0"}, indent=2),
                                       encoding="utf-8")
    (defn / "report.json").write_text(json.dumps({
        "$schema": SCHEMAS["report"],
        "themeCollection": {"customTheme": {"name": "GCDC_Theme.json", "reportVersionAtImport": {
            "visual": "2.4.0", "page": "2.0.0", "report": "3.0.0"}, "type": "RegisteredResources"}},
        "resourcePackages": [{"name": "RegisteredResources", "type": "RegisteredResources",
                              "items": [{"name": "GCDC_Theme.json", "path": "GCDC_Theme.json", "type": "CustomTheme"}]}],
        "settings": {"useStylableVisualContainerHeader": True, "exportDataMode": "AllowSummarized",
                     "defaultDrillFilterOtherVisuals": True, "allowChangeFilterTypes": True, "useEnhancedTooltips": True,
                     "useDefaultAggregateDisplayName": True}}, indent=2), encoding="utf-8")
    pages = _pages()
    (defn / "pages" / "pages.json").write_text(json.dumps(
        {"$schema": SCHEMAS["pages"], "pageOrder": [p.key for p in pages], "activePageName": pages[0].key}, indent=2),
        encoding="utf-8")
    for p in pages:
        pdir = defn / "pages" / p.key
        (pdir / "visuals").mkdir(parents=True)
        (pdir / "page.json").write_text(json.dumps(
            {"$schema": SCHEMAS["page"], "name": p.key, "displayName": p.name, "displayOption": "FitToPage",
             "height": p.height, "width": p.width}, indent=2), encoding="utf-8")
        for i, v in enumerate(p.visuals):
            vj = _visual_json(p.key, i, v)
            vdir = pdir / "visuals" / vj["name"]
            vdir.mkdir()
            (vdir / "visual.json").write_text(json.dumps(vj, indent=2, ensure_ascii=False), encoding="utf-8")
    return pages


def referenced_fields(pages: list[Page]) -> set[tuple[str, str, str]]:
    out = set()
    for p in pages:
        for v in p.visuals:
            for fields in v.roles.values():
                out |= {(f.table, f.name, f.kind) for f in fields}
            if v.sort:
                out.add((v.sort[0].table, v.sort[0].name, v.sort[0].kind))
            out |= {(f.table, f.name, f.kind) for f, _ in v.filters}
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", choices=["sharepoint", "local"], default="sharepoint")
    ap.add_argument("--out", default=str(ROOT / "powerbi"))
    ap.add_argument("--site-url", help="OneDrive/SharePoint URL (default: [powerbi] site_url in config)")
    ap.add_argument("--folder-path", help="export folder inside the site (default: [powerbi] folder_path)")
    ap.add_argument("--local-folder", help="export folder for --source local (default: [export] dir, absolute)")
    args = ap.parse_args(argv)
    from gcdc.config import load_config
    cfg = load_config()
    conn = connect(":memory:")
    migrate(conn)
    schema = export_schema(conn)
    out = Path(args.out)
    sm, rp = f"{NAME}.SemanticModel", f"{NAME}.Report"
    for folder in (sm, rp):
        if (out / folder).exists():
            shutil.rmtree(out / folder)
    out.mkdir(parents=True, exist_ok=True)
    build_model(out / sm, schema, args.source,
                site_url=args.site_url or cfg.get("powerbi.site_url", ""),
                folder_path=args.folder_path or cfg.get("powerbi.folder_path", "Documents/GCDC Command Centre/powerbi"),
                local_folder=args.local_folder or str(Path(cfg.export_dir).resolve()))
    pages = build_report(out / rp, sm)
    (out / f"{NAME}.pbip").write_text(json.dumps(
        {"$schema": SCHEMAS["pbip"], "version": "1.0", "artifacts": [{"report": {"path": rp}}],
         "settings": {"enableAutoRecovery": True}}, indent=2), encoding="utf-8")
    # Every field a visual uses must exist in the model.
    columns = {(table_name(t), c) for t, cols in schema.items() for c in cols}
    measures = {m.name for m in MEASURES}
    missing = [f for f in referenced_fields(pages)
               if (f[2] == "measure" and f[1] not in measures) or (f[2] == "column" and (f[0], f[1]) not in columns)]
    if missing:
        raise SystemExit(f"report references unknown fields: {missing}")
    print(json.dumps({"out": str(out), "source": args.source, "tables": len(schema) + 1, "measures": len(MEASURES),
                      "relationships": len(RELATIONSHIPS), "pages": len(pages),
                      "visuals": sum(len(p.visuals) for p in pages)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
