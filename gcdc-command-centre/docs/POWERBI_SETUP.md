# Power BI setup (manual steps)

Everything that can be automated is done. These steps happen on the GCDC PC and need the GCDC Microsoft
sign-in (`info@goldcoastdevotedcare.com.au`), so only you can do them. Allow about 40 minutes the first time.

## How the refresh works

GCDC's Microsoft 365 plan is email-only, with no SharePoint or OneDrive for Business. Microsoft reports
"Tenant does not have a SPO license". So Power BI reads the export folder **on the GCDC PC** through Microsoft's
free **on-premises data gateway (personal mode)**. The data goes only from the PC to Power BI.

```
GCDC PC (Task Scheduler)                                      Power BI Service (info@goldcoastdevotedcare.com.au)
gcdc refresh ─> C:\GCDC\gcdc-command-centre\exports\powerbi ─> personal gateway ─> scheduled refresh
```

## 1. Install the command centre on the PC (10 min)

1. Install **Python 3.11 or newer** from python.org (tick *Add python.exe to PATH*) and **Git** from git-scm.com.
2. In PowerShell:
   ```powershell
   git clone https://github.com/Ritik-goindani08/Ritiklal.github.io C:\GCDC
   cd C:\GCDC\gcdc-command-centre
   python -m venv .venv
   .venv\Scripts\pip install -e ".[import]"
   .venv\Scripts\gcdc init
   ```
   Until the pull request is merged, add `-b claude/gcdc-data-powerbi-foundation-lbjzhb` to the clone command.
3. Download each GCDC Google Sheet as Excel (*File → Download → Microsoft Excel*) into one folder, keeping
   the file names. Then run:
   ```powershell
   .venv\Scripts\gcdc import all C:\path\to\that\folder
   .venv\Scripts\gcdc refresh
   ```
   `C:\GCDC\gcdc-command-centre\exports\powerbi` should now hold about 40 `pbi_*.csv` files.

Installing somewhere other than `C:\GCDC`? Set `[powerbi] local_folder` in `config/gcdc.toml` to the full path
of the export folder and run `.venv\Scripts\python powerbi\build_pbip.py`.

## 2. Open the report in Power BI Desktop (5 min)

1. Install **Power BI Desktop** from the Microsoft Store.
2. If your version asks, enable File → Options → Preview features → **Power BI Project (.pbip) save option**
   and **Store semantic model using TMDL format**, then restart Desktop.
3. Open `C:\GCDC\gcdc-command-centre\powerbi\GCDC Command Centre.pbip` and sign in (top right) as
   `info@goldcoastdevotedcare.com.au`.
4. Click **Refresh**. Check the ten pages: Executive, Today — Action Centre, Revenue, Outreach Performance,
   Opportunity Discovery, Partnership Pipeline, Money on the Table, Worker Capacity, Regional Performance,
   Agent Health.

## 3. Publish (2 min)

**Home → Publish → My workspace.**

## 4. Install the personal gateway (5 min)

1. Go to https://app.powerbi.com signed in as `info@goldcoastdevotedcare.com.au`. Click the **Download** (↓) icon
   at the top right → **Data gateway**.
2. Run the installer and choose **On-premises data gateway (personal mode)**. The standard mode is for
   shared servers.
3. Sign in with `info@goldcoastdevotedcare.com.au` when the installer asks. It should report the gateway as online.

## 5. Connect the report to the gateway and schedule the refresh (5 min)

In app.powerbi.com → **My workspace** → the **GCDC Command Centre** semantic model → **⋯ → Settings**:

1. **Gateway and cloud connections**: the personal gateway shows as *Online*. Select it and **Apply**.
2. **Data source credentials → Edit credentials** for the folder `C:\GCDC\...\exports\powerbi`:
   - Authentication method **Windows**.
   - Enter the Windows sign-in of this PC. For a Microsoft account, use its email and password. For a local
     account, use `PCNAME\username`.
   - Privacy level **Organizational** → **Sign in**.
3. **Refresh → Configure a refresh schedule**:
   - On, time zone **(UTC+10:00) Brisbane**.
   - Times 07:30, 08:30, 10:30, 12:30, 14:30, 16:30, 18:30. These run after the PC's exports; the 07:30
     refresh picks up the morning brief.
   - Tick **Send refresh failure notifications**.
4. Click **Refresh now** once and check that the "Data as at" card on the Executive page updates.

Sharing the report with other people requires Power BI Pro (or Premium Per User) licences. Power BI tells
you if your plan needs one for anything else.

## 6. Schedule the PC side (2 min)

```powershell
powershell -ExecutionPolicy Bypass -File scripts\windows\register_tasks.ps1
# add -IncludeMailboxSync after completing "Mailbox sync" below, and -BriefWithAI to use Claude for the brief
```

This registers the daily backup (06:15), the refresh (06:30, then hourly 08:00–18:00) and the Daily Brief
(weekdays 07:00). **The PC must be on and awake at the Power BI refresh times.** Set Settings → System → Power
→ *When plugged in, put my device to sleep after: Never*. A refresh that runs while the PC sleeps fails, and
the next one catches up.

## Later: refresh without the PC (optional)

If GCDC adds SharePoint/OneDrive to its Microsoft 365 plan (for example Microsoft 365 Business Basic), Power BI
can refresh from the cloud without the gateway or an awake PC:

1. Set `[export] dir` to a OneDrive-synced folder.
2. Set `[powerbi] source = "sharepoint"` with `site_url` and `folder_path`.
3. Run `python powerbi/build_pbip.py`.
4. Open the project in Desktop, republish, and set the credentials to OAuth2.

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

If Microsoft 365 was bought through a reseller (e.g. GoDaddy), app registration may need the reseller's admin
portal or support to grant access.

## Daily brief with Claude (optional)

`pip install -e ".[ai]"`, set `ANTHROPIC_API_KEY` for the Windows user, and register the tasks with
`-BriefWithAI`. The model only chooses and explains the focus items. The numbers still come from the database.
Requests use Anthropic's server-side refusal fallback (`fallbacks: "default"`). Without a key the brief falls
back to the rules and says so.

## Troubleshooting

| Symptom | Fix |
|---|---|
| Desktop: "We couldn't find the folder" | Check the `ExportLocalFolder` parameter (Transform data → Edit parameters) points at the folder that holds `pbi_meta.csv`, and that `gcdc refresh` has run. |
| Service: "gateway offline" / refresh failed | The PC was off or asleep, or the gateway app was closed. Open *On-premises data gateway (personal mode)* on the PC and sign in again if asked. |
| Service: credentials error | Semantic model → Settings → Data source credentials → Edit → Windows → re-enter the PC sign-in. |
| Numbers look old | Check the "Data as at" card. If old, look at `logs\gcdc-YYYY-MM.log` and the refresh history in Power BI. |
| Refresh fails after a code update | Run `python powerbi/build_pbip.py`, open the project in Desktop and republish (the columns changed). |
