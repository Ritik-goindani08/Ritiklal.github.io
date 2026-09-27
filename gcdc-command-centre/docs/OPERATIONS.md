# Operations

## Daily cycle (automatic once scheduled)

| Time (Brisbane) | Task | Command |
|---|---|---|
| 06:00–20:00 every 30 min (optional) | Outlook sync | `gcdc sync outlook` |
| 06:15 | Backup | `gcdc backup` |
| 06:30 | Refresh: config sync, reconcile, data-quality, yesterday's snapshot, export | `gcdc refresh` |
| 07:00 weekdays | Daily BI Agent (then re-exports so Power BI shows today's focus) | `gcdc brief` |
| 08:00–18:00 hourly | Refresh | `gcdc refresh` |
| Power BI Service, from 07:30 | Scheduled refresh through the personal gateway (PC must be awake) | (configured in the Service) |

Windows: `scripts\windows\register_tasks.ps1` (`-Unregister` removes the tasks). Linux/macOS: `scripts/unix/gcdc.crontab`.
Output goes to `logs\gcdc-YYYY-MM.log`. Every run also appears on the **Agent Health** page.

## Changing business settings

Edit `config/gcdc.toml`, then run `gcdc config sync` (the next refresh also does it). No code changes are needed.

- **Targets:** `[targets] mrr`, `weekly_hours_min`, `weekly_hours_max`. Each change is stored as a new version
  effective from today, so history keeps the old target.
- **What counts to MRR:** `[revenue] mrr_rate_bases`, `weeks_per_month`.
- **Pipeline:** `[pipeline] near_term_min_stage`, `[pipeline.stage_probability]`.
- **Action Centre windows, bounce threshold, follow-up spacing:** `[action_centre]`, `[outreach]`.
- **Agents and expected run intervals:** `[agents.*]`. Add a block to monitor a new agent.
- **Public holidays:** `[calendar] public_holidays`. **Update each year** from the Queensland Government list.

## Checks

```bash
gcdc status          # headline numbers
gcdc dq              # run data-quality checks now
gcdc brief --force --no-save   # preview today's brief
```

## Backup and restore

`gcdc backup` makes a consistent copy with SQLite's online backup API into `[database] backup_dir` and keeps
`backup_keep` (30) daily copies. Point `backup_dir` at a Google Drive or OneDrive folder for an off-machine copy. To restore,
stop the scheduled tasks, copy a backup over `data/gcdc.db` (delete any `gcdc.db-wal`/`-shm` files), then run
`gcdc refresh`.

## Fixing data

- **Duplicate organisation:** `gcdc merge-orgs --keep <id> --merge <id>`. History moves and future records resolve to the kept one.
- **Wrong or missing record:** re-send it with the same key (`source_key`, message id, `roster_ref`, `invoice_number`).
  The ingest layer updates in place.
- **Manual records** (workers, rosters, revenue, referrals, …): see [DATA_ENTRY](DATA_ENTRY.md).
- Never edit the export CSVs. Every refresh overwrites them.

## Upgrading

`git pull`, `pip install -e .`, then `gcdc init` (applies new migrations and rebuilds views). If the
reporting views changed, run `python powerbi/build_pbip.py`, open the project in Desktop and republish.
`pytest` should pass before any upgrade is scheduled.

## Troubleshooting

| Symptom | Where to look |
|---|---|
| An agent is RED | Agent Health page → "Why" column; `agent_runs.last_error`; the agent's own log |
| REFRESH_PIPELINE AMBER | Usually data-quality errors: Agent Health → data-quality table |
| "database is locked" | Another process holds a write transaction. The tasks use `MultipleInstances IgnoreNew`; check for a stuck `gcdc` process. |
| Brief did not arrive | Business day? (`[calendar] public_holidays`). Check `logs\` and the DAILY_BI row on Agent Health. |
| Numbers differ between Power BI and the brief | They read the same views. Compare "Data as at": the Service may not have refreshed yet. |
