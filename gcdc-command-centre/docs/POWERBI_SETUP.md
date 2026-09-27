# Power BI setup (manual steps)

Everything that can be automated is done. These steps need your Microsoft sign-in, so only you can do them.
Allow about 30 minutes the first time.

## How the refresh works

```
PC (Task Scheduler)                     OneDrive for Business                 Power BI Service
gcdc refresh ─> exports\powerbi\*.csv ─> syncs to SharePoint Online  ─>  scheduled refresh (no gateway)
```

Power BI Service reads the CSVs from OneDrive in the cloud, so **no on-premises data gateway is needed**.

## 1. Put the export folder in OneDrive (5 min)

1. In File Explorer, open your OneDrive for Business folder (e.g. `OneDrive - Gold Coast Devoted Care`)
   and create `GCDC Command Centre\powerbi`.
2. In `config/gcdc.toml`, set the export folder to that local path:
   ```toml
   [export]
   dir = "C:/Users/<you>/OneDrive - Gold Coast Devoted Care/GCDC Command Centre/powerbi"
   ```
   Keep `[database] path` **outside** OneDrive. Only the export (and optionally backups) should sync.
3. Run `gcdc refresh`. About 40 `pbi_*.csv` files and `manifest.json` appear, and OneDrive uploads them.

## 2. Tell the report where the folder is (2 min)

1. Open OneDrive in a browser. The address looks like
   `https://<tenant>-my.sharepoint.com/personal/<your_account>/_layouts/15/onedrive.aspx`.
2. Copy everything up to and including `/personal/<your_account>/` into `config/gcdc.toml`:
   ```toml
   [powerbi]
   site_url = "https://<tenant>-my.sharepoint.com/personal/<your_account>/"
   folder_path = "Documents/GCDC Command Centre/powerbi"
   ```
3. Regenerate the project so these become the defaults: `python powerbi/build_pbip.py`.
   You can also skip this and set them later in Power BI Desktop under Transform data → Edit parameters.

## 3. Open the report in Power BI Desktop (10 min)

1. Install or update **Power BI Desktop** (Microsoft Store, Windows).
2. If your version asks, enable File → Options → Preview features → **Power BI Project (.pbip) save option**
   and **Store semantic model using TMDL format**, then restart Desktop.
3. Open `gcdc-command-centre\powerbi\GCDC Command Centre.pbip`.
4. When prompted for SharePoint credentials, choose **Microsoft account / Organizational account** →
   **Sign in** with the GCDC Microsoft 365 account → **Connect**. Apply at the site level.
5. Click **Refresh**. Check the ten pages: Executive, Today — Action Centre, Revenue, Outreach
   Performance, Opportunity Discovery, Partnership Pipeline, Money on the Table, Worker Capacity,
   Regional Performance, Agent Health.

Desktop-only alternative (no OneDrive): `python powerbi/build_pbip.py --source local` builds the same report
reading the local export folder. That version cannot refresh in the Service without a gateway.

## 4. Publish and schedule the refresh (10 min)

1. In Desktop: **Home → Publish** → choose a workspace (My workspace, or a GCDC workspace).
2. In app.powerbi.com: open the workspace → the **GCDC Command Centre** semantic model → **Settings**.
3. **Data source credentials → Edit credentials**: Authentication method **OAuth2**, privacy level
   **Organizational** → **Sign in**.
4. **Refresh → Configure a refresh schedule**: On; time zone **(UTC+10:00) Brisbane**; add times after the
   PC's exports, e.g. 07:30, 08:30, 10:30, 12:30, 14:30, 16:30, 18:30. Tick **Send refresh failure notifications**.
   The 07:30 refresh picks up the morning brief. The Daily BI Agent re-exports after it runs at 07:00, so its
   focus items appear in the "Today's focus" table on the Today page.
5. Optional: pin the Executive page to a dashboard, or install the Power BI mobile app.

Sharing the report with anyone else requires Power BI Pro (or Premium Per User) licences for them and you.

## 5. Schedule the PC side (2 min)

In PowerShell from the project folder:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\windows\register_tasks.ps1
# add -IncludeMailboxSync after completing "Mailbox sync" below, and -BriefWithAI to use Claude for the brief
```

This registers the daily backup (06:15), the refresh (06:30, then hourly 08:00–18:00) and the Daily Brief
(weekdays 07:00). Tasks run while you are signed in, and missed runs catch up when the PC wakes.

## Mailbox sync (optional, about 10 min)

Records sent emails, replies and bounces from Outlook automatically. It is read-only and uses fixed rules, not AI.

1. Go to https://entra.microsoft.com → **App registrations → New registration**: name `GCDC Mailbox Sync`,
   *Accounts in this organizational directory only*, no redirect URI → Register.
2. **Authentication → Advanced settings → Allow public client flows: Yes** → Save.
3. **API permissions → Add a permission → Microsoft Graph → Delegated → Mail.Read** → Add.
   If your tenant requires it, **Grant admin consent**.
4. Copy the **Directory (tenant) ID** and **Application (client) ID** into `config/gcdc.toml` under `[graph]`.
5. `pip install -e ".[graph]"`, then run `gcdc sync outlook --days 30` once. Follow the printed
   instruction (open https://microsoft.com/devicelogin, enter the code, sign in as the GCDC mailbox).
   The token is cached in `data/`, so scheduled runs need no sign-in.

## Daily brief with Claude (optional)

`pip install -e ".[ai]"`, set `ANTHROPIC_API_KEY` for the Windows user, and register the tasks with
`-BriefWithAI`. The model only chooses and explains the focus items. The numbers still come from the database.
Requests use Anthropic's server-side refusal fallback (`fallbacks: "default"`). Without a key the brief falls
back to the rules and says so.

## Troubleshooting

| Symptom | Fix |
|---|---|
| Desktop: "We couldn't find the folder" | Check `ExportSiteUrl` ends with `/personal/<account>/` and `ExportFolderPath` starts with `Documents/`. The folder must contain `pbi_meta.csv`. |
| Service: credentials error | Semantic model → Settings → Data source credentials → Edit → OAuth2 → Sign in again. |
| Numbers look old | Check the "Data as at" card. If old, look at `logs\gcdc-YYYY-MM.log` and the OneDrive sync icon. |
| Refresh fails after a code update | Run `python powerbi/build_pbip.py` and republish (the columns changed). |
