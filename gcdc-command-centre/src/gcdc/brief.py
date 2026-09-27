"""Daily BI Agent — the morning management brief.

Answers one question: what should Ritik do today that is most likely to move
GCDC toward recurring revenue?

Everything in the brief is computed deterministically from the same views the
Power BI report uses (pbi_kpi_current, pbi_action_centre, the fact views), so
the brief and the dashboard can never disagree. The optional --ai step only
*chooses and explains* today's focus from a list of candidates this module has
already computed; it cannot add facts or change numbers, and any failure falls
back to the deterministic choice.

Runs on business days (weekends and configured QLD public holidays are skipped
unless forced). Each run is logged as agent DAILY_BI, saved to daily_briefs
(exported to Power BI as pbi_daily_brief) and written to <briefs_dir>/<date>.md.
"""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any

from gcdc import __version__
from gcdc.agents import agent_run
from gcdc.config import Config
from gcdc.db import pinned_now, transaction
from gcdc.quality import CHECK_NAMES
from gcdc.timeutil import is_business_day, local_today, local_tz, now_iso

# Agents whose failure stops outreach or the data behind the dashboard.
CRITICAL_AGENTS = {"OUTREACH", "INBOX", "MAILBOX_SYNC", "REFRESH_PIPELINE", "PRE_SEND_CHECKER"}


@dataclass
class Candidate:
    """One possible focus for today, with the evidence that justifies it."""

    id: str
    category: str
    title: str
    why: str
    score: float
    evidence: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------
def _money(v: float | None) -> str:
    return "UNKNOWN" if v is None else f"${v:,.0f}"


def _num(v: float | None) -> str:
    if v is None:
        return "UNKNOWN"
    return f"{v:,.0f}" if float(v).is_integer() else f"{v:,.1f}"


def _pct(v: float | None) -> str:
    return "n/a" if v is None else f"{v * 100:.0f}%"


def _plural(n: int, word: str, plural: str | None = None) -> str:
    return f"{n} {word if n == 1 else (plural or word + 's')}"


def _prev_business_day(d: date, holidays: frozenset[str]) -> date:
    p = d - timedelta(days=1)
    while not is_business_day(p, holidays):
        p -= timedelta(days=1)
    return p


def _rows(conn: sqlite3.Connection, sql: str, *params) -> list[dict]:
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def _one(conn: sqlite3.Connection, sql: str, *params) -> Any:
    row = conn.execute(sql, params).fetchone()
    return row[0] if row else None


# ---------------------------------------------------------------------------
# Facts: everything the brief says, gathered once
# ---------------------------------------------------------------------------
def gather_facts(conn: sqlite3.Connection, cfg: Config, brief_date: date) -> dict[str, Any]:
    holidays = cfg.public_holidays
    since = _prev_business_day(brief_date, holidays)
    k = dict(conn.execute("SELECT * FROM pbi_kpi_current").fetchone())
    week_ago = conn.execute("SELECT mrr, weekly_billable_hours, active_participants FROM daily_metrics_snapshot "
                            "WHERE snapshot_date <= ? ORDER BY snapshot_date DESC LIMIT 1",
                            ((brief_date - timedelta(days=7)).isoformat(),)).fetchone()
    # Activity since the previous business day (e.g. Friday + weekend on a Monday).
    window = (since.isoformat(), (brief_date - timedelta(days=1)).isoformat())
    outreach = dict(conn.execute(
        "SELECT COALESCE(SUM(is_first_touch_sent), 0) AS first_touches, COALESCE(SUM(is_follow_up_sent), 0) AS follow_ups, "
        "COALESCE(SUM(CASE WHEN bounced_date BETWEEN ? AND ? THEN 1 ELSE 0 END), 0) AS bounces "
        "FROM pbi_fact_outreach WHERE sent_date BETWEEN ? AND ? OR bounced_date BETWEEN ? AND ?",
        (*window, *window, *window)).fetchone())
    replies = dict(conn.execute(
        "SELECT COALESCE(SUM(is_human_reply), 0) AS replies, COALESCE(SUM(is_positive), 0) AS positive "
        "FROM pbi_fact_reply WHERE received_date BETWEEN ? AND ?", window).fetchone())
    actions = _rows(conn, "SELECT category, priority, organisation_name, opportunity_title, next_action, due_status, "
                          "days_overdue, potential_revenue_display, potential_monthly_value, action_score "
                          "FROM pbi_action_centre ORDER BY action_score DESC, action_key")
    agents = _rows(conn, "SELECT agent_code, agent_name, rag_status, rag_reason FROM pbi_fact_agent_health "
                         "ORDER BY rag_sort, agent_name")
    dq = _rows(conn, "SELECT check_code, severity, COUNT(*) AS n FROM pbi_fact_data_quality "
                     "GROUP BY check_code, severity ORDER BY severity_sort, n DESC")
    trials_unconverted = _rows(conn, """
        SELECT t.participant_ref, t.trial_date, t.status, o.name AS organisation_name
        FROM pbi_fact_trial_shift t LEFT JOIN organisations o ON o.organisation_id = t.organisation_id
        WHERE t.is_scheduled_or_done = 1 AND COALESCE(t.is_converted, 0) = 0
          AND COALESCE(t.outcome, 'PENDING') <> 'NOT_CONVERTED'
          AND NOT EXISTS (SELECT 1 FROM rosters r JOIN participants p ON p.participant_id = r.participant_id
                          WHERE p.participant_ref = t.participant_ref AND r.status IN ('ACTIVE', 'SCHEDULED'))
        ORDER BY t.trial_date""")
    unapproved_hours = _one(conn, "SELECT COALESCE(SUM(active_weekly_hours), 0) FROM pbi_fact_roster "
                                  "WHERE active_rate_not_approved = 1")
    open_requirements = _rows(conn, "SELECT region_code, required_capability, weekly_hours, needed_by "
                                    "FROM pbi_fact_worker_requirement WHERE is_open = 1 ORDER BY needed_by")
    week = ((brief_date - timedelta(days=7)).isoformat(), (brief_date - timedelta(days=1)).isoformat())
    attempted_7d, bounced_7d = conn.execute(
        "SELECT COALESCE(SUM(is_attempted), 0), COALESCE(SUM(is_bounced), 0) FROM pbi_fact_outreach "
        "WHERE sent_date BETWEEN ? AND ?", week).fetchone()
    unconfirmed = _one(conn, "SELECT COUNT(*) FROM outreach WHERE status = 'SENT' AND evidence = 'SHEET_PLAN' "
                             "AND sent_at >= datetime('now', '-30 days')")
    last_refresh = _one(conn, "SELECT MAX(finished_at) FROM agent_runs WHERE agent_code = 'REFRESH_PIPELINE' "
                              "AND status IN ('SUCCESS', 'WARNING')")
    return {
        "brief_date": brief_date.isoformat(), "since": since.isoformat(), "kpi": k,
        "week_ago": dict(week_ago) if week_ago else None, "outreach_since": outreach, "replies_since": replies,
        "actions": actions, "agents": agents, "dq": dq, "trials_unconverted": trials_unconverted,
        "unapproved_roster_hours": unapproved_hours, "open_worker_requirements": open_requirements,
        "last_refresh_utc": last_refresh, "unconfirmed_sheet_sends_30d": unconfirmed,
        "bounces_7d": {"attempted": attempted_7d, "bounced": bounced_7d,
                       "limit": float(cfg.get("outreach.bounce_rate_warning", 0.05))},
    }


# ---------------------------------------------------------------------------
# Focus candidates: closest-to-revenue first
# ---------------------------------------------------------------------------
def _first(actions: list[dict], *categories: str) -> list[dict]:
    return [a for a in actions if a["category"] in categories]


def focus_candidates(f: dict[str, Any], cfg: Config) -> list[Candidate]:
    k, actions = f["kpi"], f["actions"]
    out: list[Candidate] = []

    def urgency(a: dict) -> float:
        return {"OVERDUE": 10 + min(20, (a.get("days_overdue") or 0)), "DUE_TODAY": 8}.get(a.get("due_status"), 0)

    refs = _first(actions, "Referral")
    if refs:
        a = refs[0]
        more = f" ({_plural(len(refs) - 1, 'more referral')} waiting)" if len(refs) > 1 else ""
        out.append(Candidate("referral", "Referral", f"Action the referral from {a['organisation_name']}: "
                             f"{a['next_action']}{more}",
                             "A referral is one step from recurring hours; slow responses lose participants to "
                             "other providers.", 100 + urgency(a), {"referrals_needing_action": len(refs), **a}))
    if f["trials_unconverted"]:
        t = f["trials_unconverted"][0]
        out.append(Candidate("trial", "Trial shift", f"Turn the trial for {t['participant_ref']}"
                             f"{' (' + t['organisation_name'] + ')' if t['organisation_name'] else ''} into a "
                             f"recurring roster: confirm days, hours, rate and start date",
                             "A trial without a roster earns nothing recurring; converting it adds MRR directly.",
                             95, {"trials_without_roster": len(f["trials_unconverted"]), **t}))
    if k["mrr_rosters_rate_unknown"]:
        n, hrs = k["mrr_rosters_rate_unknown"], f["unapproved_roster_hours"]
        out.append(Candidate("rate", "Rate approval", f"Get an approved rate for {_plural(n, 'active roster')} "
                             f"({_num(hrs)} hrs/week already delivered)",
                             "These hours are worked but excluded from MRR until the rate is approved in writing.",
                             90, {"rosters": n, "weekly_hours": hrs}))
    if f["open_worker_requirements"] and (refs or f["trials_unconverted"]):
        r = f["open_worker_requirements"][0]
        out.append(Candidate("worker", "Worker capacity", f"Fill the open worker requirement in {r['region_code']}"
                             f"{' (' + r['required_capability'] + ')' if r['required_capability'] else ''}",
                             "Without a matched worker a referral or trial cannot start.", 85,
                             {"open_requirements": len(f["open_worker_requirements"]), **r}))
    positives = _first(actions, "Positive reply")
    waiting = _first(actions, "Reply awaiting response", "Reply overdue")
    if positives or waiting:
        a = (positives or waiting)[0]
        total = len(positives) + len(waiting)
        out.append(Candidate("reply", "Replies", f"Answer {a['organisation_name']} and propose a meeting"
                             + (f" — {_plural(total, 'reply', 'replies')} waiting in total" if total > 1 else ""),
                             "A warm reply left unanswered goes cold; a meeting is the next step to a referral.",
                             80 + urgency(a) + (5 if positives else 0), {"positive": len(positives),
                                                                          "awaiting": len(waiting), **a}))
    meetings = _first(actions, "Meeting today")
    if meetings:
        a = meetings[0]
        out.append(Candidate("meeting", "Meeting", f"Prepare for today's meeting with {a['organisation_name']}: "
                             "ask for a specific participant referral and agree the next step",
                             "Meetings convert to referrals when they end with a concrete ask.", 78,
                             {"meetings_today": len(meetings), **a}))
    apps = _first(actions, "Application to complete")
    if apps:
        a = apps[0]
        out.append(Candidate("application", "Partnership", f"Complete the {a['organisation_name']} provider "
                             f"application ({_plural(len(apps), 'application')} open)",
                             "Approved associate-provider panels send recurring work without cold outreach.",
                             60 + urgency(a), {"applications": len(apps), **a}))
    followups = _first(actions, "Follow-up")
    if k["followups_overdue"]:
        a = followups[0] if followups else {}
        out.append(Candidate("followups", "Follow-ups", f"Clear {_plural(k['followups_overdue'], 'overdue follow-up')}"
                             + (f", starting with {a['organisation_name']}" if a.get("organisation_name") else ""),
                             "Most replies come from the follow-up, not the first email.",
                             55 + min(20, k["followups_overdue"]), {"overdue": k["followups_overdue"], **a}))
    p1 = _first(actions, "P1 opportunity")
    if p1:
        a = p1[0]
        out.append(Candidate("p1", "P1 targets", f"Work the P1 call list ({_plural(len(p1), 'target')} with an overdue "
                             f"next action), starting with {a['organisation_name']}: {a['next_action']}",
                             "P1 targets are the biggest potential sources of subcontracted and referred hours; "
                             "their planned calls have not happened yet.",
                             65 + urgency(a), {"p1_overdue": len(p1), **a}))
    if k["approvals_pending"]:
        out.append(Candidate("approvals", "Approvals", f"Approve or edit {_plural(k['approvals_pending'], 'queued email')}",
                             "Queued outreach does not go out until you approve it.", 50,
                             {"pending": k["approvals_pending"]}))
    red = [a for a in f["agents"] if a["rag_status"] == "RED" and a["agent_code"] in CRITICAL_AGENTS]
    if red:
        out.append(Candidate("agents", "Automation", f"Fix {red[0]['agent_name']}: {red[0]['rag_reason']}",
                             "Outreach and the numbers in this brief depend on it.", 45,
                             {"red_agents": [a["agent_code"] for a in red]}))
    week_min = float(cfg.get("brief.first_touches_per_week_min", 25))
    if k["positive_conversations_30d"] < int(cfg.get("brief.positive_conversations_30d_min", 3)) \
            and (k["emails_sent_week"] or 0) < week_min:
        out.append(Candidate("volume", "Outreach", f"Send new first touches to P1 targets (only "
                             f"{_plural(k['emails_sent_week'] or 0, 'email')} this week, "
                             f"{_plural(k['positive_conversations_30d'], 'positive conversation')} in 30 days)",
                             "The pipeline is too thin to reach the revenue target without more conversations.",
                             40, {"emails_sent_week": k["emails_sent_week"],
                                  "positive_30d": k["positive_conversations_30d"], "p1_open": k["p1_open"]}))
    out.sort(key=lambda c: (-c.score, c.id))
    return out


def choose_focus(candidates: list[Candidate], n: int = 3) -> list[tuple[Candidate, str]]:
    return [(c, c.why) for c in candidates[:n]]


# ---------------------------------------------------------------------------
# Optional Claude step: choose and explain, never invent
# ---------------------------------------------------------------------------
_AI_SYSTEM = (
    "You are the Daily BI Agent for Gold Coast Devoted Care (GCDC), a small NDIS and aged-care support provider "
    "on the Gold Coast. The owner, Ritik, reads your brief each business-day morning. Goal: grow recurring "
    "revenue (weekly billable hours x approved rate) toward the monthly target. From the candidate actions "
    "provided (each computed from the business database), choose the one to three that are most likely to move "
    "GCDC toward recurring revenue today, most important first. Base every reason only on the facts provided; "
    "if a number is UNKNOWN, treat it as unknown. Keep each reason to one or two plain sentences."
)


def ai_focus(candidates: list[Candidate], facts: dict[str, Any], cfg: Config) -> tuple[list[tuple[Candidate, str]], str]:
    import anthropic

    ids = [c.id for c in candidates]
    schema = {
        "type": "object",
        "properties": {
            "choices": {"type": "array", "items": {
                "type": "object",
                "properties": {"candidate_id": {"type": "string", "enum": ids},
                               "why_today": {"type": "string"}},
                "required": ["candidate_id", "why_today"], "additionalProperties": False}},
        },
        "required": ["choices"], "additionalProperties": False,
    }
    k = facts["kpi"]
    context = {
        "date": facts["brief_date"],
        "revenue": {key: k[key] for key in ("mrr", "revenue_target", "revenue_gap", "weekly_billable_hours",
                                            "weekly_hours_target_min", "weekly_hours_target_max",
                                            "active_participants", "mrr_rosters_rate_unknown")},
        "pipeline": {key: k[key] for key in ("active_opportunities", "p1_open", "near_term_monthly_value_known",
                                             "near_term_unknown_count", "positive_conversations_30d",
                                             "meetings_booked_30d", "referrals_30d", "trial_shifts_30d")},
        "candidates": [asdict(c) for c in candidates],
    }
    client = anthropic.Anthropic()
    response = client.beta.messages.create(
        model=cfg.get("brief.ai_model", "claude-opus-5"),
        max_tokens=16000,
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        output_config={"effort": cfg.get("brief.ai_effort", "high"),
                       "format": {"type": "json_schema", "schema": schema}},
        system=_AI_SYSTEM,
        messages=[{"role": "user", "content": "Facts (JSON):\n" + json.dumps(context, default=str, sort_keys=True)}],
    )
    if response.stop_reason == "refusal":
        raise RuntimeError("the model declined the request")
    text = next(b.text for b in response.content if b.type == "text")
    by_id = {c.id: c for c in candidates}
    chosen, seen = [], set()
    for item in json.loads(text)["choices"]:
        cid = item["candidate_id"]
        if cid in by_id and cid not in seen and len(chosen) < 3:
            seen.add(cid)
            chosen.append((by_id[cid], item["why_today"].strip() or by_id[cid].why))
    if not chosen:
        raise RuntimeError("no valid choices returned")
    return chosen, f"claude ({response.model})"


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
def render(facts: dict[str, Any], focus: list[tuple[Candidate, str]], *, generator: str,
           reconstructed: bool = False, max_actions: int = 5) -> str:
    k = facts["kpi"]
    d = date.fromisoformat(facts["brief_date"])
    lines = [f"# GCDC Daily Brief — {d.strftime('%a %d %b %Y')}",
             f"_Data as at {k['as_of_local']} (Brisbane)"
             + (" — reconstructed for a past date; activity recorded later may be included" if reconstructed else "")
             + "_", ""]

    lines.append("## Recommended focus")
    if focus:
        for i, (c, why) in enumerate(focus, 1):
            lines.append(f"{i}. **{c.title}** — {why}")
    else:
        lines.append("Nothing urgent in the data. Use the morning for new first touches to P1 targets.")
    lines.append("")

    lines.append("## Revenue (actual)")
    target = k["revenue_target"]
    trend = ""
    if facts["week_ago"] and facts["week_ago"]["mrr"] is not None:
        delta = (k["mrr"] or 0) - facts["week_ago"]["mrr"]
        trend = f", {'+' if delta >= 0 else '−'}{_money(abs(delta))} vs a week ago"
    lines.append(f"- MRR **{_money(k['mrr'])}** of {_money(target)} target ({_pct(k['mrr_pct_of_target'])}); "
                 f"gap {_money(k['revenue_gap'])}{trend}")
    lines.append(f"- Recurring weekly hours **{_num(k['weekly_billable_hours'])}** (target "
                 f"{_num(k['weekly_hours_target_min'])}–{_num(k['weekly_hours_target_max'])}); average approved rate "
                 f"{_money(k['avg_billable_rate']) if k['avg_billable_rate'] is not None else 'UNKNOWN'}"
                 f"{'/hr' if k['avg_billable_rate'] is not None else ''}")
    lines.append(f"- {_plural(k['active_participants'], 'active participant')}, "
                 f"{_plural(k['active_rosters'], 'active roster')}"
                 + (f", {_plural(k['scheduled_rosters'], 'confirmed start')} (projected MRR {_money(k['projected_mrr'])})"
                    if k["scheduled_rosters"] else ""))
    lines.append(f"- Received this month {_money(k['revenue_received_mtd'])}; invoiced {_money(k['revenue_invoiced_mtd'])}")
    if k["mrr_rosters_rate_unknown"]:
        lines.append(f"- {_plural(k['mrr_rosters_rate_unknown'], 'active roster')} "
                     f"({_num(facts['unapproved_roster_hours'])} hrs/week) excluded from MRR: rate not approved")
    lines.append("")

    lines.append("## Pipeline (estimated — not revenue)")
    lines.append(f"- {_plural(k['active_opportunities'], 'open opportunity', 'open opportunities')} "
                 f"(P1 {k['p1_open']} · P2 {k['p2_open']} · P3 {k['p3_open']})")
    lines.append(f"- Qualified near-term: {_money(k['near_term_monthly_value_known'])}/mo known + "
                 f"{k['near_term_unknown_count']} UNKNOWN; all open: {_money(k['pipeline_monthly_value_known'])}/mo known "
                 f"+ {k['pipeline_value_unknown_count']} UNKNOWN")
    lines.append(f"- Last 30 days: {_plural(k['positive_conversations_30d'], 'positive conversation')}, "
                 f"{_plural(k['meetings_booked_30d'], 'meeting')}, {_plural(k['referrals_30d'], 'referral')}, "
                 f"{_plural(k['trial_shifts_30d'], 'trial shift')}")
    lines.append("")

    lines.append("## Urgent actions")
    top = facts["actions"][:max_actions]
    if top:
        for a in top:
            when = {"OVERDUE": f"overdue {a['days_overdue']}d" if a.get("days_overdue") else "overdue",
                    "DUE_TODAY": "due today"}.get(a["due_status"], (a["due_status"] or "").lower().replace("_", " "))
            money = f" · {a['potential_revenue_display']}" if a.get("potential_monthly_value") else ""
            org = f"{a['organisation_name']}: " if a.get("organisation_name") else ""
            lines.append(f"- [{a['priority']}] {a['category']} — {org}{a['next_action']} ({when}{money})")
        rest = len(facts["actions"]) - len(top)
        if rest > 0:
            lines.append(f"- …and {rest} more on the Today page of the dashboard")
    else:
        lines.append("- Nothing due.")
    lines.append("")

    o, r = facts["outreach_since"], facts["replies_since"]
    since = date.fromisoformat(facts["since"])
    label = "yesterday" if since == d - timedelta(days=1) else f"since {since.strftime('%a %d %b')}"
    lines.append("## Outreach")
    lines.append(f"- {label[0].upper() + label[1:]}: {_plural(o['first_touches'], 'first touch', 'first touches')}, "
                 f"{_plural(o['follow_ups'], 'follow-up')}, {_plural(r['replies'], 'reply', 'replies')} "
                 f"({r['positive']} positive), {_plural(o['bounces'], 'bounce')}")
    lines.append(f"- This week {_plural(k['emails_sent_week'] or 0, 'email')} sent; today "
                 f"{k['emails_scheduled_today']} scheduled, {_plural(k['followups_due_today'], 'follow-up')} due, "
                 f"{k['approvals_pending']} awaiting approval")
    lines.append(f"- Reply rate {_pct(k['reply_rate'])}, positive {_pct(k['positive_reply_rate'])} "
                 f"of {_plural(k['orgs_contacted'], 'organisation')} contacted")
    lines.append("")

    lines.append("## Risks")
    risks = []
    others = [a for a in facts["agents"] if a["agent_code"] != "DAILY_BI"]  # this run is still in progress
    red = [a for a in others if a["rag_status"] == "RED"]
    amber = [a for a in others if a["rag_status"] == "AMBER"]
    if red:
        risks.append("RED automations: " + "; ".join(f"{a['agent_name']} ({a['rag_reason']})" for a in red[:4])
                     + (f"; +{len(red) - 4} more" if len(red) > 4 else ""))
    if amber:
        risks.append(f"AMBER automations: {', '.join(a['agent_name'] for a in amber)}")
    if k["replies_awaiting_response"]:
        risks.append(f"{_plural(k['replies_awaiting_response'], 'reply', 'replies')} awaiting your response")
    if k["followups_overdue"]:
        risks.append(f"{_plural(k['followups_overdue'], 'follow-up')} overdue")
    b = facts["bounces_7d"]
    if b["attempted"] and b["bounced"] / b["attempted"] > b["limit"]:
        risks.append(f"Bounce rate {_pct(b['bounced'] / b['attempted'])} over the last 7 days ({b['bounced']} of "
                     f"{b['attempted']}), above the {_pct(b['limit'])} limit — verify emails before sending more")
    elif k["bounces_recent"]:
        risks.append(f"{_plural(k['bounces_recent'], 'bounce')} in the last 7 days")
    errors = [x for x in facts["dq"] if x["severity"] == "ERROR"]
    if errors:
        risks.append("Data-quality errors: " + "; ".join(f"{CHECK_NAMES.get(x['check_code'], x['check_code'])} ({x['n']})"
                                                         for x in errors))
    if k["unfilled_rosters"]:
        risks.append(f"{_plural(k['unfilled_rosters'], 'roster')} without an assigned worker")
    if facts["unconfirmed_sheet_sends_30d"]:
        risks.append(f"{_plural(facts['unconfirmed_sheet_sends_30d'], 'send')} in the last 30 days come from planning "
                     "sheets and are not yet confirmed by the mailbox (connect mailbox sync to verify)")
    if facts["last_refresh_utc"] is None:
        risks.append("The refresh pipeline has never completed — dashboard data may be stale")
    lines += [f"- {x}" for x in risks] or ["- None flagged."]
    lines.append("")
    lines.append(f"_Generated by gcdc {__version__} ({generator}). Actual revenue and estimated pipeline are "
                 "reported separately and never added together._")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def run_brief(conn: sqlite3.Connection, cfg: Config, brief_date: date | None = None, *, use_ai: bool = False,
              save: bool = True, force: bool = False) -> dict[str, Any]:
    off = cfg.utc_offset_hours
    today = local_today(off)
    brief_date = brief_date or today
    if not force and not is_business_day(brief_date, cfg.public_holidays):
        return {"skipped": True, "brief_date": brief_date.isoformat(),
                "markdown": f"No brief: {brief_date.strftime('%a %d %b %Y')} is not a business day "
                            "(use --force to run anyway).\n"}
    if brief_date > today:
        raise ValueError("cannot brief a future date")
    reconstructed = brief_date < today
    at = datetime.combine(brief_date, time(7, 0), tzinfo=local_tz(off)) if reconstructed else None
    with agent_run(conn, "DAILY_BI", trigger="MANUAL" if force or reconstructed else "SCHEDULED") as run:
        with pinned_now(conn, at, off):
            facts = gather_facts(conn, cfg, brief_date)
        candidates = focus_candidates(facts, cfg)
        focus, generator, ai_note = choose_focus(candidates), "deterministic rules", None
        if use_ai and candidates:
            try:
                focus, generator = ai_focus(candidates, facts, cfg)
            except Exception as exc:  # the brief must still go out
                ai_note = f"AI step skipped ({type(exc).__name__}: {exc}); using deterministic focus"
                run.warn(ai_note)
        markdown = render(facts, focus, generator=generator, reconstructed=reconstructed,
                          max_actions=int(cfg.get("brief.max_actions", 5)))
        if ai_note:
            markdown += f"\n_{ai_note}_\n"
        payload = {"facts": facts, "candidates": [asdict(c) for c in candidates],
                   "focus": [{"id": c.id, "title": c.title, "why": why} for c, why in focus], "generator": generator}
        path = None
        if save:
            with transaction(conn):
                conn.execute(
                    "INSERT INTO daily_briefs (brief_date, generated_at, generator, brief_markdown, brief_json) "
                    "VALUES (?, ?, ?, ?, ?) ON CONFLICT(brief_date) DO UPDATE SET generated_at = excluded.generated_at, "
                    "generator = excluded.generator, brief_markdown = excluded.brief_markdown, "
                    "brief_json = excluded.brief_json",
                    (brief_date.isoformat(), now_iso(), generator, markdown, json.dumps(payload, default=str)))
            out_dir = cfg.briefs_dir
            out_dir.mkdir(parents=True, exist_ok=True)
            path = out_dir / f"{brief_date.isoformat()}.md"
            tmp = path.with_suffix(".md.tmp")
            tmp.write_text(markdown, encoding="utf-8")
            os.replace(tmp, path)
            run.created = 1
        run.processed = len(candidates)
        run.details = {"brief_date": brief_date.isoformat(), "generator": generator,
                       "focus": [c.id for c, _ in focus]}
    return {"skipped": False, "brief_date": brief_date.isoformat(), "markdown": markdown,
            "path": str(path) if path else None, "generator": generator, "focus": payload["focus"]}
