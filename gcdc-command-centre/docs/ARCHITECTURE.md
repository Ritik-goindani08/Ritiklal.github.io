# Architecture

## Principles

1. **One source of truth.** A single SQLite database (`data/gcdc.db`) holds every organisation, contact,
   opportunity, touch, reply, meeting, referral, trial, roster, invoice, worker, partnership, agent run
   and alert. Nothing else is authoritative. Google Sheets, agent logs and the mailbox are *inputs*;
   Power BI and the brief are *outputs*.
2. **One write path.** Every writer (agents, importers, the Outlook sync, people using CSV templates)
   goes through `gcdc.ingest`. It validates, resolves the organisation, deduplicates, and records
   who wrote what (`source_system`, `created_by`, `agent_run_id`).
3. **Logic in SQL, not in the dashboard.** The `pbi_*` views compute every flag, stage, value and
   rank. Power BI measures only add up precomputed columns. The daily snapshot and the brief read the
   same views, so the three can never disagree.
4. **Deterministic data movement.** Importing, syncing, reconciling, snapshotting and exporting are
   ordinary code. No model moves or transforms a row. The only AI step is optional: `gcdc brief --ai`
   chooses among focus candidates the rules already computed.
5. **Unknown stays unknown.** A missing rate or hours value is NULL and displays as UNKNOWN. It is never
   zero and never estimated. Actual revenue and estimated pipeline live in different tables and columns.

## Components

```
                ┌──────────────────────── writers ────────────────────────┐
 Agents ── gcdc ingest (JSON) / Python API ─┐                             │
 Existing sheets ── gcdc import ────────────┤                             │
 Outlook ── gcdc sync outlook (Graph) ──────┼─> gcdc.ingest ──> SQLite ◄─┘
 CSV templates ── gcdc import canonical ────┤    validate            │
 Legacy agent DB ── gcdc legacy import ─────┘    resolve org          │
                                                 idempotent upsert    │
                                                 stage evidence       │
                                                                      ▼
 gcdc refresh (scheduled) ── sync config ─ reconcile ─ data quality ─ snapshot ─ export CSV ─> personal gateway ─> Power BI Service
 gcdc brief   (weekdays)  ── pbi_kpi_current + pbi_action_centre ─> focus rules (+ optional Claude choice) ─> Markdown + DB
```

| Module | Responsibility |
|---|---|
| `db.py` | Connection (WAL, foreign keys), numbered migrations, views rebuilt on start, config sync, "as-of" pinning, backup |
| `identity.py` | Name keys, ABN checksum, registrable domains, email normalisation, organisation matching |
| `ingest.py` | One upsert per record type; natural keys; suppression enforcement; stage advancement |
| `reconcile.py` | Links stray events to opportunities, closes satisfied follow-ups, marks answered replies |
| `quality.py` | 21 data-quality checks → `data_quality_results` + alerts (auto-resolving) |
| `snapshot.py` | Append-only `daily_metrics_snapshot`, catching up any missed days |
| `export.py` | Atomic CSV export of every `pbi_*` view + date dimension + manifest with column types |
| `pipeline.py` | The scheduled refresh, logged as agent `REFRESH_PIPELINE` |
| `brief.py` | Daily BI Agent |
| `importers/` | Existing Google Sheets, canonical CSVs, Microsoft Graph mailbox, legacy SQLite adoption |
| `powerbi/build_pbip.py` | Generates the Power BI project from the export schema |

## Why SQLite, CSV and a personal gateway

- The agents run on one Windows PC. SQLite needs no server, supports concurrent readers with WAL,
  and gives transactional writes and constraints. The schema is plain SQL and ports to PostgreSQL or
  Azure SQL if GCDC outgrows one machine.
- Power BI Service reaches the CSV export on the PC through Microsoft's free on-premises data gateway
  (personal mode). GCDC's Microsoft 365 plan has no SharePoint/OneDrive for Business, so a cloud copy of the
  export is not available. The generator also supports that route (`[powerbi] source = "sharepoint"`,
  no gateway) if a SharePoint plan is added later. The export is atomic (temp file + rename), so a refresh
  never reads a half-written file.
- Keep the live database outside any cloud-synced folder. Sync tools can corrupt an open SQLite file.
  `gcdc backup` copies it safely, and backup copies can live in Google Drive or OneDrive.

## Time

All timestamps are stored in UTC (`...Z`). Business dates ("today", "due", "this week") are calculated in
Brisbane time (UTC+10, no daylight saving). `runtime_context` pins "now" for the duration of a refresh,
so every view in one export agrees on the same moment. A pin expires after 30 minutes if a process dies.

## Failure behaviour

- A bad record in a batch is rejected with its error. The rest of the batch still lands (savepoint per record).
- Every agent run is logged. A run that crashes is recorded as FAILED with the traceback, raises an alert and
  turns the agent RED on the Agent Health page.
- The refresh is idempotent. Running it twice exports the same data and never writes a second snapshot for
  a day. Missed days are caught up and flagged `is_catch_up`.
