# Audit of existing GCDC data (before this build)

The brief was to reuse and extend what exists, not to start a parallel dataset. This records what was found
and how each source was brought into the central database. Business figures are deliberately left out of
this public repository.

## Sources found

| Source | What it held | How it is used now |
|---|---|---|
| GCDC Outreach Agent database (local SQLite on the agents' PC) | The agent's own record of targets and sends | Not reachable from the build environment. `gcdc legacy audit` inventories it read-only and suggests a mapping; `gcdc legacy import` brings it in through the ingest layer (idempotent). The agent then writes to the central database instead. |
| GCDC_Email_Campaign_Lists (sheet, one tab per audience) | The Outreach Agent's working lists: company, ABN, website, email, tier, evidence, dated "Email 1/2 sent" columns | `gcdc import campaign-lists` |
| GCDC_Coordinator_Emails | A three-touch support-coordinator sequence with dated sends and notes | `gcdc import coordinator-emails` |
| GCDC_Referral_CRM | The call CRM: status, last contact, next follow-up, owner, notes | `gcdc import referral-crm` (the richest conversation history) |
| GCDC_Support_at_Home_Provider_Database | Aged-care associate-provider targets with decision-makers and contact pages | `gcdc import sah-provider-db` |
| GCDC Outreach Queue | Planned Support-at-Home contact tasks | `gcdc import outreach-queue` |
| GCDC_Target_List | Associate-provider / panel applications and the compliance pack | `gcdc import target-list` → partnerships, applications, compliance documents |
| crm-master.csv (Power-BI-Projects repository) | Scored leads from an earlier commercial opportunity audit | `gcdc import crm-master`: imported as a source, **not** kept as a second Power BI dataset |
| GCDC Facebook Intent Radar | Discovery sheet of posts showing intent | `gcdc import facebook-radar` (worked examples skipped) |
| Outlook mailbox | The only reliable record of what was actually sent and received | `gcdc sync outlook` (Microsoft Graph, read-only, deterministic) |

No source held rosters, participants, invoices, worker checks or availability. Those start empty and are
entered with the templates in `templates/` (see DATA_ENTRY) or by the Worker Capacity agent. The dashboard
therefore shows $0 MRR until real rosters exist. It does not show an estimate instead.

## Problems found and the rule adopted

| Finding | Rule in the importers / ingest layer |
|---|---|
| The same organisation appears in several sheets under slightly different names, domains or emails | One identity resolver for everything (ABN → domain → email → name key); conflicting ABNs never merge |
| "Status" columns were set once and never maintained | Ignored. Dated columns and the mailbox are the evidence. |
| Past planned-send dates were never confirmed | Imported as SENT with `evidence = SHEET_PLAN`; the mailbox sync confirms them instead of duplicating; the brief and a data-quality check show how many are unconfirmed |
| Future planned dates | Imported as SCHEDULED |
| Placeholder values ("TBC", "Not publicly verified", "n/a", "-") | Treated as unknown and never stored as data |
| Dates earlier than the business started outreach (typos) | Rejected and reported (`business.records_start_date`) |
| A sheet note says a reply came in but has no date | Recorded as a reply dated at the last known touch, flagged `received_at_is_estimate` |
| A generic CRM category ("NDIS reg. support coordination") would have created a second opportunity for organisations already in the pipeline | Attaches to the organisation's existing opportunity (`match_any_type`) |
| CRM "call back" rows were being closed by any later email | A call-back is closed only by a phone, in-person or SMS touch after it was set (`satisfied_after` + channel rules) |
| Rates and hours were rarely known | Stay UNKNOWN; pipeline values are shown as "known $ + N UNKNOWN" |

## Result of the import (shape only)

- All eight sheets imported with zero failed records, and re-importing changes nothing (idempotent).
- Every organisation that appeared in more than one sheet became one organisation.
- Import problems (bad dates, statuses without dates) are listed in the import output rather than guessed.
- Sheet fixes worth making at source:
  - **GCDC_Coordinator_Emails:** some dated-send cells carry the wrong year (2022, 2025) or only a weekday
    name. Those touches are reported and skipped until the dates are corrected.
  - **GCDC Facebook Intent Radar:** still holds only its worked examples, so there is nothing to import yet.
  - **GCDC Outreach Queue:** the "Done? (Y/N)" and "Result" columns were never filled in. Its planned contacts
    stay open (and overdue) until they are marked done or the mailbox sync records the sends.

## Decisions

- **Extend, don't duplicate:** the Outreach Agent's database is adopted into the central database, and the
  earlier Power BI dataset (crm-master) becomes an input. Power BI has exactly one semantic model, fed by
  the central database.
- **Mailbox as ground truth for sends and replies**, sheets as ground truth only for what they uniquely hold
  (call history, research, applications).
- **Public repository:** code, schema, templates and the report definition are committed. Data, exports,
  logs and tokens are git-ignored. Consider moving the project to a private repository.
