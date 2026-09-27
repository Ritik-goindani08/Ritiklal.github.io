# GCDC Command Centre

One central business-development database for Gold Coast Devoted Care, and a Power BI report on top of it.

```
 Opportunity Discovery ─┐                                   ┌─> exports/powerbi/*.csv ─> Power BI (10 pages)
 Outreach Agent ────────┤   gcdc ingest / Python API        │     (OneDrive folder, refreshed hourly)
 Inbox / Reply Agent ───┼──> central SQLite database  ──────┤
 Worker Capacity ───────┤   (source of truth, de-duped,     └─> Daily BI Agent brief (weekdays 07:00)
 Outlook (Graph sync) ──┤    idempotent, audited)
 Existing Google Sheets ┘
```

- **The database is the source of truth.** Agents write to it through one ingest layer that matches
  organisations across agents (ABN → website domain → email → name), so the same business found by
  two agents is one organisation. Re-sending a record never duplicates it.
- **Power BI is reporting only.** It reads CSV exports of `pbi_*` views; all business logic
  (MRR, stages, conversion flags, action ranking, agent health) lives in SQL so the dashboard, the
  daily snapshot and the brief always agree.
- **Money is never invented.** MRR = active rosters × weekly hours × *approved* rate × 4.333333.
  Unknown hours or rates stay UNKNOWN. Actual revenue and estimated pipeline are separate fields in
  separate tables and are never added together.
- **Everything that moves data is deterministic code.** The only optional AI step is `gcdc brief --ai`,
  which picks today's focus from candidates the rules already computed.

## Quick start

Python 3.11+.

```bash
cd gcdc-command-centre
python -m venv .venv
.venv/bin/pip install -e .                  # Windows: .venv\Scripts\pip install -e .
.venv/bin/pip install -e ".[import]"        # to import the existing .xlsx sheets

gcdc init                                   # creates data/gcdc.db and loads config/gcdc.toml
gcdc import all path/to/downloaded-sheets   # optional: bring in the existing Google Sheets
gcdc refresh                                # reconcile, data-quality, snapshot, export
gcdc status                                 # headline numbers in the terminal
gcdc brief                                  # the Daily BI Agent brief
```

To preview the dashboard with clearly-fictional data (never mix with the real database):

```bash
GCDC_DB=data/demo.db GCDC_EXPORT_DIR=exports/demo gcdc demo seed
GCDC_DB=data/demo.db GCDC_EXPORT_DIR=exports/demo gcdc refresh
```

Then follow [docs/POWERBI_SETUP.md](docs/POWERBI_SETUP.md) to open the report.

## What's here

| Path | What |
|---|---|
| `config/gcdc.toml` | Every business setting: targets ($5,000 MRR, 20–25 h/week), MRR method, stage rules, thresholds, agent schedules, public holidays. Edit and run `gcdc config sync` — no code changes. |
| `src/gcdc/sql/migrations/` | Database schema (numbered migrations; applied automatically). |
| `src/gcdc/sql/views/` | Business logic and the `pbi_*` reporting views (rebuilt on every start). |
| `src/gcdc/ingest.py` | The only write path for agents: validation, de-duplication, idempotency, stage evidence. |
| `src/gcdc/importers/` | Existing Google Sheets, CSV templates, Outlook (Microsoft Graph), legacy agent databases. |
| `src/gcdc/brief.py` | Daily BI Agent. |
| `powerbi/` | The Power BI project (`GCDC Command Centre.pbip`) and its generator `build_pbip.py`. |
| `templates/` | CSV templates for entering workers, rosters, revenue, referrals and more by hand. |
| `scripts/` | Windows Task Scheduler and cron schedules. |
| `tests/` | `pytest` suite (schema, identity, ingest, revenue rules, snapshots, DQ, importers, export, PBIP, brief). |

## Commands

| Command | Purpose |
|---|---|
| `gcdc ingest FILE --agent OUTREACH` | Agents write records (JSON / JSON-lines / stdin). `--dry-run` validates only. |
| `gcdc run start/finish` | Log runs for agents that are not Python (feeds Agent Health). |
| `gcdc check-send --to EMAIL` | Pre-send check: suppression, bounces, duplicates, replies waiting. |
| `gcdc import KIND FILE` | Import existing sheets (`all`, `referral-crm`, …), `canonical` CSVs, `mailbox-json`. |
| `gcdc sync outlook` | Deterministic Outlook sync (sent, replies, bounces) via Microsoft Graph. |
| `gcdc legacy audit/import DB` | Adopt the existing Outreach Agent database with a mapping file. |
| `gcdc refresh` | Config sync → reconcile → data-quality checks → daily snapshot → export. |
| `gcdc brief [--ai]` | Morning brief (business days only unless `--force`). |
| `gcdc status`, `gcdc dq`, `gcdc snapshot`, `gcdc export`, `gcdc merge-orgs` | Operations. |

## Documentation

- [ARCHITECTURE](docs/ARCHITECTURE.md): components, data flow, design decisions
- [DATA_MODEL](docs/DATA_MODEL.md): tables, IDs, the journey from found to revenue, identity resolution
- [METRICS](docs/METRICS.md): every KPI definition, MRR methodology, pipeline rules, action score, RAG rules
- [AGENT_INTEGRATION](docs/AGENT_INTEGRATION.md): how each agent writes to the database
- [POWERBI_SETUP](docs/POWERBI_SETUP.md): the manual Power BI and Microsoft steps
- [OPERATIONS](docs/OPERATIONS.md): schedules, logs, backup, troubleshooting
- [DATA_ENTRY](docs/DATA_ENTRY.md): entering workers, rosters and revenue by hand
- [AUDIT](docs/AUDIT.md): what existed before and how it was reused

## Tests

```bash
pip install -e ".[dev]"
pytest
# Optional: validate the report JSON against Microsoft's published schemas
GCDC_PBIR_SCHEMAS=/path/to/microsoft/json-schemas pytest tests/test_export.py
```

## Data safety

`data/`, `exports/`, `logs/` and the Graph token cache are git-ignored. Business data (organisations,
contacts, participants, revenue) must never be committed — this repository is public.
