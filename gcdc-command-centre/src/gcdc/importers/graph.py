"""Deterministic Outlook sync via Microsoft Graph (no AI): sent items, replies, bounces.

One-time setup (manual, ~5 minutes — see docs/POWERBI_SETUP.md#mailbox-sync):
  1. Entra admin centre -> App registrations -> New registration
     "GCDC Mailbox Sync", single tenant, public client / native.
  2. Authentication -> Allow public client flows = Yes.
  3. API permissions -> Microsoft Graph -> Delegated -> Mail.Read -> Grant admin consent.
  4. Put tenant_id and client_id under [graph] in config/gcdc.toml.
  5. `pip install -e .[graph]` then run `gcdc sync outlook` once interactively
     (device code sign-in as info@goldcoastdevotedcare.com.au). The refresh token
     is cached locally, so scheduled runs are unattended afterwards.
"""

from __future__ import annotations

import json
import sqlite3
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from gcdc.config import Config

GRAPH = "https://graph.microsoft.com/v1.0"
SCOPES = ["Mail.Read"]
_SELECT = ("id,internetMessageId,conversationId,subject,from,sender,toRecipients,sentDateTime,"
           "receivedDateTime,bodyPreview,isDraft")


def _token(cfg: Config) -> str:
    try:
        import msal
    except ImportError as exc:
        raise SystemExit("Mailbox sync needs msal:  pip install -e .[graph]") from exc
    tenant, client = cfg.get("graph.tenant_id"), cfg.get("graph.client_id")
    if not tenant or not client:
        raise SystemExit("Set [graph] tenant_id and client_id in config/gcdc.toml (see docs/POWERBI_SETUP.md)")
    cache_path = cfg._path(cfg.get("graph.token_cache", "data/.graph_token_cache.json"))
    cache = msal.SerializableTokenCache()
    if cache_path.exists():
        cache.deserialize(cache_path.read_text())
    app = msal.PublicClientApplication(client, authority=f"https://login.microsoftonline.com/{tenant}",
                                       token_cache=cache)
    accounts = app.get_accounts(username=cfg.get("graph.mailbox"))
    result = app.acquire_token_silent(SCOPES, account=accounts[0]) if accounts else None
    if not result:
        flow = app.initiate_device_flow(scopes=SCOPES)
        if "user_code" not in flow:
            raise SystemExit(f"device flow failed: {flow}")
        print(flow["message"], flush=True)  # one-time interactive sign-in
        result = app.acquire_token_by_device_flow(flow)
    if "access_token" not in result:
        raise SystemExit(f"sign-in failed: {result.get('error_description', result)}")
    if cache.has_state_changed:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(cache.serialize())
    return result["access_token"]


def _get_all(url: str, token: str) -> list[dict]:
    items: list[dict] = []
    while url:
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}",
                                                   "Prefer": 'outlook.body-content-type="text"'})
        with urllib.request.urlopen(req, timeout=60) as resp:
            doc = json.loads(resp.read().decode())
        items.extend(doc.get("value", []))
        url = doc.get("@odata.nextLink")
    return items


def fetch_messages(cfg: Config, days: int) -> list[dict]:
    token = _token(cfg)
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    messages = []
    for folder, field in (("sentitems", "sentDateTime"), ("inbox", "receivedDateTime")):
        query = urllib.parse.urlencode({"$filter": f"{field} ge {since}", "$select": _SELECT, "$top": "100",
                                        "$orderby": f"{field} asc"})
        messages += _get_all(f"{GRAPH}/me/mailFolders/{folder}/messages?{query}", token)
    return messages


def sync_outlook(conn: sqlite3.Connection, cfg: Config, *, days: int = 3) -> dict:
    from gcdc.importers.mailbox import mailbox_records
    from gcdc.importers.runner import _ingest

    owner = cfg.get("graph.mailbox")
    if not owner:
        raise SystemExit("Set [graph] mailbox in config/gcdc.toml")
    messages = fetch_messages(cfg, days)
    records, ignored = mailbox_records(conn, messages, owner=owner, free_mail=cfg.free_mail_domains)
    result, run_id = _ingest(conn, cfg, records, agent="MAILBOX_SYNC", system="mailbox", dry_run=False)
    return {"messages": len(messages), "records": len(records), "ignored_unrelated": ignored, "run_id": run_id,
            **{k: result[k] for k in ("failed", "created", "updated", "stats")}}


def dump_messages(cfg: Config, days: int, out: Path) -> int:
    """Save raw Graph messages (for `gcdc import mailbox-json`)."""
    messages = fetch_messages(cfg, days)
    out.write_text(json.dumps({"mailbox": cfg.get("graph.mailbox"), "messages": messages}, indent=1))
    return len(messages)
