# Data model

## The journey and its IDs

```
organisations ──< opportunities ──< outreach ──< replies
      │                 │  (stage history)          │
      │                 ├──< meetings               └─ response_outreach_id (our answer)
      │                 ├──< participant_referrals ──< trial_shifts
      │                 └──< rosters >── participants ──< revenue_transactions
      ├──< contacts
      ├──< partnerships ──< associate_provider_applications, partnership_documents
      └──< follow_ups
 workers ──< worker_availability;  rosters.primary_worker_id, trial_shifts.worker_id
```

Found → Contacted → Reply → Conversation → Meeting → Referral → Trial → Recurring roster → Revenue are all
linked by integer IDs. Every event row carries `organisation_id` and, where known, `opportunity_id`.
Revenue is attributed through its roster to the participant, opportunity and organisation that produced it
(`pbi_fact_revenue.organisation_id` / `opportunity_id`), so the source of every dollar is traceable.

## Tables

| Area | Tables |
|---|---|
| Organisations | `organisations`, `organisation_identifiers` (ABN, domain, email, name key, external ids), `contacts` |
| Pipeline | `opportunities`, `opportunity_keys` (extra idempotency keys), `opportunity_stage_history` (trigger-maintained) |
| Outreach | `outreach`, `follow_ups`, `replies`, `meetings`, `suppression` |
| Delivery | `participant_referrals`, `trial_shifts`, `participants`, `rosters`, `revenue_transactions` |
| Workforce | `workers`, `worker_availability`, `worker_requirements` |
| Partnerships | `partnerships`, `partnership_documents`, `associate_provider_applications`, `compliance_documents` |
| Operations | `agents`, `agent_runs`, `alerts`, `data_quality_runs`, `data_quality_results`, `daily_metrics_snapshot`, `daily_briefs` |
| Configuration | `settings`, `business_targets` (versioned), `ref_*` code tables, `runtime_context` |

Schema: `src/gcdc/sql/migrations/0001_core_schema.sql`. Codes: `0002_reference_data.sql`.

## Organisation identity (no duplicates across agents)

When any record names an organisation, `identity.find_organisation` tries, in order:

1. **External id**, e.g. `sah_db:123` or `legacy:leads:42`
2. **ABN** (only if the checksum is valid)
3. **Registrable website/email domain** (`www.care.example.com.au` → `example.com.au`). Free-mail domains
   (gmail, bigpond, outlook, …, listed in config) are never used as an identity.
4. **Exact email address**
5. **Name key**: lower-case, punctuation removed, "Pty Ltd" / "Limited" / "The" / "Inc" dropped

A match is rejected when both sides carry **different ABNs**. Franchises and same-named businesses stay
separate. Every identifier seen is registered to the organisation, so later records match more reliably.
Duplicates that slip through (for example the same business under two domains) show up in the data-quality
checks. `gcdc merge-orgs --keep A --merge B` then moves all history to one record, and later records
naming B resolve to A.

## Idempotency: re-sending never duplicates

| Record | Natural key |
|---|---|
| organisation | identity resolution above |
| contact | email |
| opportunity | `(source_system, source_key)`, or `opportunity_keys` |
| outreach, reply | `external_message_id` (mailbox Message-ID), else `(source_system, source_key)` |
| meeting, referral, trial, follow-up, application, requirement | `(source_system, source_key)` |
| participant / roster / worker | `participant_ref` / `roster_ref` / `worker_ref` |
| revenue | `invoice_number` (required, or a `source_key`) |

## Stages come from evidence

`opportunities.stage_code` is what an agent or person recorded. The **effective stage** (`pbi_fact_opportunity.stage_code`)
is the furthest of the recorded stage and the furthest stage the linked records prove:

| Stage | Proven by |
|---|---|
| FOUND | the opportunity exists |
| VERIFIED | `is_verified = 1` |
| CONTACTED | an outreach with status SENT |
| REPLIED | a reply that is not an out-of-office/auto-reply |
| POSITIVE | a reply classified POSITIVE or REFERRAL |
| MEETING | a meeting BOOKED, COMPLETED or RESCHEDULED |
| REFERRAL | a participant referral |
| TRIAL | a trial shift SCHEDULED or COMPLETED |
| ROSTER | a roster (scheduled, active, paused or ended) |

Stages never move backwards automatically. A recorded stage ahead of its evidence is flagged by the
`STAGE_AHEAD_OF_EVIDENCE` data-quality check. Status (OPEN, WON, LOST, ON_HOLD, DISQUALIFIED) is separate
from stage.

## History

- `daily_metrics_snapshot`: one row per day, append-only. Triggers reject UPDATE and DELETE.
- `business_targets`: every change to a target in config adds a new version effective from that day.
  Old versions stay, so past snapshots keep the target that applied then.
- `opportunity_stage_history`: every stage change, maintained by triggers.
- `agent_runs`, `data_quality_runs`: full history (data-quality detail is kept for 60 days).

## Reporting views

`pbi_*` views are the only interface Power BI sees. `gcdc export` writes each one to CSV, adds `pbi_dim_date`
and `pbi_dim_dq_check`, and records every column's type in `manifest.json`. `powerbi/build_pbip.py`
reads the same type registry, so the model and the files cannot drift.
