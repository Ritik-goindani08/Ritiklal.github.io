# Entering records by hand

Some facts only exist in your head or in invoices: workers and their checks, participants, rosters,
revenue, referrals received by phone, meetings, trials, partnerships, and your compliance pack. Enter them
with the CSV templates in `templates/`.

```bash
copy templates\roster.csv my_rosters.csv     # one template per record type
# fill rows in Excel, delete the EXAMPLE row (rows marked EXAMPLE are always skipped), save as CSV
gcdc import canonical my_rosters.csv --entity roster --dry-run   # check
gcdc import canonical my_rosters.csv --entity roster             # write
```

Re-importing the same file updates records in place. The keys are `worker_ref`, `participant_ref`,
`roster_ref`, `invoice_number` and `source_key`. Blank cells are ignored, never written as blank.

| Template | Entity | Notes |
|---|---|---|
| `worker.csv` | worker | Enter only **verified** checks (1 = seen the document). Capacity counts only fully verified ACTIVE workers. |
| `worker_availability.csv` | worker_availability | `day_of_week` 1 = Monday … 7 = Sunday. Times are 24-hour; overnight shifts may end next day. |
| `participant.csv` | participant | Use an internal reference (e.g. P-001), **not** the participant's name. |
| `roster.csv` | roster | `rate_basis` must be `APPROVED` for the roster to count toward MRR. Use QUOTED or EXPECTED until the rate is confirmed in writing. |
| `revenue.csv` | revenue | One row per invoice, amounts ex GST. Update `status`/`paid_date` when paid by re-importing the same `invoice_number`. |
| `referral.csv` | referral | Leave `expected_hourly_rate` blank if unknown. It stays UNKNOWN, never zero. |
| `meeting.csv`, `trial_shift.csv` | meeting, trial_shift | `source_key` identifies the row for later updates. |
| `partnership.csv`, `application.csv` | partnership, application | Associate-provider and subcontracting relationships. |
| `compliance_document.csv` | compliance_document | Your own insurance, ABN, policies. Drives the "Compliance pack" card. |
| `worker_requirement.csv` | worker_requirement | Unfilled needs by region, capability and time band. |
| `suppression.csv` | suppression | Do-not-contact emails, domains or organisations. |

Codes (regions, service types, funding types, statuses) accept the code or its display name, e.g.
`Gold Coast` or `GOLD_COAST`. An invalid value is rejected with the list of valid ones.

**Privacy:** participant and worker records identify people. Keep the database and exports on GCDC
devices and GCDC's Microsoft 365 only. Never commit them to git.
