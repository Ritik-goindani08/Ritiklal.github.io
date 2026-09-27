# Metrics and rules

All numbers are computed in SQL views (`src/gcdc/sql/views/`). Settings are in `config/gcdc.toml`.

## Revenue (actual)

| Metric | Definition |
|---|---|
| **MRR** | Σ over ACTIVE rosters with known hours and an **approved** rate: `weekly_hours × hourly_rate × weeks_per_month` |
| weeks_per_month | 4.333333 (52 ÷ 12), `[revenue] weeks_per_month` |
| Approved rate | `rate_basis` in `[revenue] mrr_rate_bases` (default only `APPROVED`). QUOTED/EXPECTED rates never count. |
| Rosters excluded from MRR | Active rosters with no rate or a non-approved rate. Their hours still count as billable hours and they are listed so the rate can be approved. |
| Projected MRR | MRR plus SCHEDULED rosters (confirmed future starts) with an approved rate |
| Weekly billable hours | Σ weekly_hours of ACTIVE rosters |
| Average billable rate | Σ(hours × rate) ÷ Σ hours over the rosters that count to MRR (same basis as MRR). UNKNOWN if none. |
| Revenue target / gap | `[targets] mrr` (versioned); gap = max(0, target − MRR) |
| Weekly hours target | `[targets] weekly_hours_min`–`weekly_hours_max` (20–25) |
| Revenue received this month | Σ `amount_paid` with `paid_date` in the current month |
| Revenue invoiced this month | Σ `amount_ex_gst` (not VOID/WRITTEN_OFF) with service date in the current month |
| Active participants | Distinct participants on ACTIVE rosters |

Revenue is always ex GST. An invoice needs an `invoice_number` (or `source_key`) so it can never be counted twice.

## Pipeline (estimated, never added to actual revenue)

| Metric | Definition |
|---|---|
| Estimated weekly/monthly value | `expected_weekly_hours × expected_hourly_rate` (× weeks_per_month). **Only when both are known**. Otherwise NULL with `value_status` = `UNKNOWN_HOURS`, `UNKNOWN_RATE` or `UNKNOWN`. |
| Qualified near-term pipeline | Open opportunities at or beyond `[pipeline] near_term_min_stage` (MEETING) |
| Longer-term pipeline | Open opportunities before that stage |
| Pipeline display | Always "known $ + N UNKNOWN". Unknown values are counted, not guessed. |
| Weighted pipeline | Known value × `[pipeline.stage_probability]`. These are business rules, not predictions. Set `use_stage_probabilities = false` to hide. |
| Money on the Table | The open-opportunity table: hours, rate, rate basis, weekly/monthly value, value status, stage, rule probability, horizon |

## Funnel and conversion

- Funnel counts use the effective stage (see DATA_MODEL): an opportunity "reached" every stage up to its
  furthest evidence.
- Conversion rates are organisation-based: organisations that replied (or met, referred, trialled, rostered)
  ÷ organisations contacted.
- **Recurring hours per 100 organisations contacted** = active weekly hours from rosters whose organisation
  was contacted ÷ organisations contacted × 100.
- **Revenue per 100 organisations contacted** = actual revenue from contacted organisations ÷ organisations
  contacted × 100.
- Sends imported from planning sheets carry `evidence = SHEET_PLAN` until the mailbox sync confirms them.

## Action Centre ranking

```
action_score = priority weight   (P1 300, P2 200, P3 100)
             + category weight   (referral 60, positive reply 55, meeting today 50, reply 45, approval 40,
                                  agent failure 35, follow-up 30, agent not reporting 30, application 25,
                                  compliance 22, partnership 20, P1 stalled 15, bounce 5)
             + 5 × days overdue  (capped at 30 days)
             + known monthly value ÷ 100  (capped at 50)
```

Potential revenue shows only when it is known. Otherwise the row shows UNKNOWN. Response windows and the
look-ahead are set in `[action_centre]`.

## Agent health (GREEN / AMBER / RED)

For each agent in `[agents.*]`, with `interval_hours` = expected gap between successful runs:

| Status | Rule |
|---|---|
| RED | Last run FAILED/ABORTED, or last success older than interval × `red_multiplier` (3.0), or never reported |
| AMBER | Last run finished with warnings, or last success older than interval × `amber_multiplier` (1.5), or a first run is in progress |
| GREEN | Otherwise |

Agents with `business_days_only = true` get weekend allowance, so they do not turn AMBER on Monday morning.

## Data-quality checks

| Code | Severity | What it finds |
|---|---|---|
| DUP_ORG_NAME / DUP_ORG_DOMAIN | WARNING | Likely duplicate organisations |
| DUP_ORG_ABN | ERROR | Two organisations with the same ABN |
| MISSING_ORG_LINK | ERROR | Records not linked to a live organisation |
| OPP_NO_SOURCE | WARNING | Opportunities without a source |
| UNVERIFIED_EMAIL_SCHEDULED / UNVERIFIED_EMAILS | WARNING / INFO | Scheduled sends to unverified addresses; unverified contacts |
| STAGE_AHEAD_OF_EVIDENCE / INVALID_STAGE | WARNING / ERROR | Recorded stage not backed by records / invalid stage or status combination |
| REVENUE_NO_SOURCE | ERROR | Revenue with no roster, participant or organisation behind it |
| PARTICIPANT_NO_ROSTER | ERROR | Active participant without an active or scheduled roster |
| ROSTER_RATE_UNKNOWN | WARNING | Active roster without an approved rate |
| IMPOSSIBLE_DATES | ERROR | Sends, replies or discoveries in the future; dates before `records_start_date`; rosters or participants ending before they start |
| DUP_OUTREACH | WARNING | Duplicate outreach records |
| SENT_TO_SUPPRESSED | ERROR | A send to a suppressed address was recorded |
| SENT_UNCONFIRMED | INFO | Sheet-planned sends not yet confirmed by the mailbox |
| WORKER_NOT_VERIFIED / ROSTER_UNVERIFIED_WORKER | ERROR | Active worker missing required checks / rostered anyway |
| WORKER_CHECK_EXPIRING | WARNING | A worker's screening, first aid, CPR or other check expires within 30 days |
| AGENCY_MANAGED_REFERRAL | WARNING | NDIA-managed referral (GCDC is not a registered provider) |
| ORG_UNKNOWN_REGION | INFO | Contacted organisations with no region |

Results appear on the Agent Health page (`Fact Data Quality`). ERROR checks raise alerts that resolve
automatically once fixed.

## Daily snapshot

`daily_metrics_snapshot` stores the end-of-day values of the KPIs above plus the day's activity (emails,
replies, meetings, referrals, trials, received revenue). One row per date, never updated. The refresh writes
yesterday's row and catches up any missed days (`is_catch_up = 1`).

## Daily BI Agent: recommended focus

Candidates are ranked closest-to-revenue first: referral to action → trial to convert → roster rate to
approve → worker to match → reply to answer → meeting today → P1 calls → partnership application →
overdue follow-ups → approvals → broken critical automation → outreach volume (if the pipeline is thin).
Overdue items get an urgency bonus. The top three become the brief's focus. With `--ai`, Claude picks and
explains up to three from the same candidate list. It cannot add candidates or numbers. The chosen items
are exported as `pbi_daily_focus` and shown at the top of the Today page.
