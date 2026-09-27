# Connecting the agents

Every agent writes through the same ingest layer. Agents never need their own copy of organisations:
they send what they know, and the database matches it to the existing organisation or creates one.

## Three ways to write

**1. CLI with JSON (any language).** Write a JSON array, a `{"records": [...]}` object, or JSON-lines:

```bash
gcdc ingest today.json --agent OUTREACH            # logs an OUTREACH run automatically
gcdc ingest - --agent INBOX < replies.jsonl         # from stdin
gcdc ingest batch.json --agent OUTREACH --dry-run   # validate only, write nothing
```

The command prints `processed / succeeded / failed / created / updated / errors / warnings` and exits
non-zero only if *every* record failed. A bad record is reported and skipped. The rest still land.

**2. Python API (agents written in Python).**

```python
from gcdc.config import load_config
from gcdc.db import open_db, transaction
from gcdc.agents import agent_run
from gcdc.ingest import IngestContext, ingest_records

cfg = load_config()                       # config/gcdc.toml (or $GCDC_CONFIG)
conn = open_db(cfg)                       # data/gcdc.db (or $GCDC_DB)
with agent_run(conn, "OPPORTUNITY_DISCOVERY") as run:      # FAILED + alert if this block raises
    ctx = IngestContext(conn, cfg, agent_code="OPPORTUNITY_DISCOVERY", run_id=run.run_id)
    with transaction(conn):
        result = ingest_records(ctx, records)
    run.processed, run.created, run.updated = result.processed, result.created, result.updated
    for e in result.errors:
        run.error(e["error"])
```

**3. Run logging only.** For an agent that writes elsewhere but should still appear on Agent Health:

```bash
RUN=$(gcdc run start --agent WEEKLY_PLANNER | python -c "import sys,json;print(json.load(sys.stdin)['run_id'])")
gcdc run finish --run-id $RUN --status SUCCESS --processed 12 --next-run 2026-10-05T07:00:00+10:00
```

## Record reference

Every record has a `type`. Organisations can be referenced by `organisation_id` or by an `organisation`
object with any of `name`, `abn`, `website`, `general_email`, `external_id`. Outreach and replies can
also be identified by the email address alone. Timestamps without a timezone are read as Brisbane time.
Codes such as regions ("Gold Coast"), stages and types accept the code or the display name.

| type | Key fields | Idempotency key |
|---|---|---|
| `organisation` | name, abn, website, general_email, region, suburb, org_category, tier, evidence_level | identity resolution |
| `contact` | organisation, email, full_name, role, email_status | email |
| `opportunity` | organisation, opportunity_type_code, source_code, source_detail, source_url, priority, is_verified, discovered_at, expected_weekly_hours, expected_hourly_rate, rate_basis, next_action, next_action_due, stage_code | source_key (+ source_system) |
| `outreach` | organisation, to_email, subject, status, scheduled_for, sent_at, touch_type, sequence_step, channel, campaign_code, external_message_id, thread_id, evidence, opportunity | external_message_id, else source_key |
| `bounce` | to_email, bounced_at, bounce_type (HARD/SOFT), bounce_reason | marks the matching outreach BOUNCED and the contact BOUNCED |
| `follow_up` | organisation, due_date, action_type, description, satisfied_after | source_key |
| `reply` | from_email, received_at, classification, summary, external_message_id, thread_id, response_status | external_message_id, else source_key |
| `meeting` | organisation, scheduled_start, meeting_type, status, mode, outcome, next_step | source_key |
| `referral` | organisation, participant_ref, received_at, funding_type, requested_weekly_hours, expected_hourly_rate, rate_basis, status, next_action | source_key |
| `trial_shift` | organisation, participant_ref, scheduled_start, hours, status, outcome, worker_ref | source_key |
| `participant` | participant_ref, status, funding_type, region_code, organisation (referrer) | participant_ref |
| `roster` | roster_ref, participant_ref, organisation, weekly_hours, hourly_rate, rate_basis, status, worker_ref, start_date | roster_ref |
| `revenue` | invoice_number, invoice_date, roster_ref / participant_ref, hours, hourly_rate, amount_ex_gst, status, paid_date | invoice_number |
| `worker`, `worker_availability`, `worker_requirement` | see `templates/` | worker_ref / (worker, day, time) / source_key |
| `partnership`, `application`, `compliance_document` | see `templates/` | organisation + type / source_key / doc_code |
| `suppression` | email / domain / organisation, reason | scope + value |
| `alert` | alert_key, severity, category, message | alert_key |

Valid codes are in `src/gcdc/sql/migrations/0002_reference_data.sql`. Enumerations are the CHECK constraints in
`0001_core_schema.sql`, and the ingest error message lists them when a value is wrong.

## Per agent

### Opportunity Discovery Agent
Send one `organisation` and one `opportunity` per find. Give each opportunity a stable `source_key` (the
listing URL or tender number) so re-discovery updates rather than duplicates. Include `source_code` (GOOGLE,
SEEK, INDEED, FACEBOOK, GOVERNMENT_TENDER, PROVIDER_WEBSITE, CAREVICINITY, …) and `source_url`. An existing
organisation found again gets a new opportunity, which the dashboard counts as "existing organisation, new
opportunity". Leave `expected_hourly_rate` empty unless a rate was actually published.

### Outreach Agent
1. Before sending, call `gcdc check-send --to x@y --org "Name"`. It blocks suppressed or bounced addresses,
   repeat sends within 3 days, and organisations with a reply waiting for Ritik.
2. Record each email as an `outreach` record: `PENDING_APPROVAL` when it needs Ritik, `SCHEDULED` with
   `scheduled_for`, then `SENT` with `sent_at` and the mailbox `external_message_id` once it has gone.
   Updates use the same `source_key` or message id.
3. Suppressed addresses are refused at ingest time (unless `--allow-suppressed`). A send that happened
   anyway raises an ERROR alert.

### Inbox / Reply Agent
Send `reply` records with the `external_message_id` and `thread_id` of the inbound email and a
`classification` (POSITIVE, REFERRAL, QUESTION, NEUTRAL, NOT_INTERESTED, UNSUBSCRIBE, OUT_OF_OFFICE, …).
The mailbox sync may already have created the reply as UNCLASSIFIED. Sending the same message id updates
the classification. UNSUBSCRIBE replies should also send a `suppression` record. Replies marked
`requires_response` stay on the Today page until an outbound `touch_type: REPLY` is recorded in the thread.

### Worker Capacity Agent
Send `worker` (only verified facts: the check flags and expiry dates), `worker_availability` and
`worker_requirement` records. A worker counts as available capacity only when ACTIVE with every check
required by `[workers]` in config verified and current.

### Outlook (no agent needed)
`gcdc sync outlook` reads Sent Items and Inbox through Microsoft Graph and records sends, replies,
auto-replies and bounces by fixed rules. It confirms sheet-planned sends instead of duplicating them.
Setup: docs/POWERBI_SETUP.md → Mailbox sync.

## Adopting the existing Outreach Agent database

```bash
gcdc legacy audit "C:\path\to\outreach_agent.db" > audit.json    # read-only inventory + suggested mapping
# copy mappings/legacy_outreach_agent.example.toml, adjust table/column names
gcdc legacy import "C:\path\to\outreach_agent.db" --mapping my_mapping.toml --dry-run
gcdc legacy import "C:\path\to\outreach_agent.db" --mapping my_mapping.toml
```

Rows go through the normal ingest layer. Each legacy primary key becomes the `source_key`, so the import
is safe to repeat. Once imported, point the agent at `gcdc ingest` (or the Python API) and stop writing to
its private database.
