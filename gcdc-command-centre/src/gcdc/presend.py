"""Pre-send check used by the Outreach / Pre-Send Checker agents before any email goes out.

`gcdc check-send --to intake@org.example --org "Org Name" --subject "..."`
exits 0 when the send is allowed and 2 when it must not go out, with reasons.
"""

from __future__ import annotations

import sqlite3

from gcdc.config import Config
from gcdc.identity import email_domain, find_organisation, normalise_email
from gcdc.ingest import suppression_hit


def check_send(conn: sqlite3.Connection, cfg: Config, *, to_email: str, organisation: str | None = None,
               subject: str | None = None, recent_days: int = 3) -> dict:
    email = normalise_email(to_email)
    blockers: list[str] = []
    warnings: list[str] = []
    if not email:
        return {"ok": False, "to": to_email, "blockers": ["not a valid email address"], "warnings": []}
    contact = conn.execute("SELECT * FROM contacts WHERE email = ?", (email,)).fetchone()
    org_id = contact["organisation_id"] if contact else None
    if org_id is None:
        found = find_organisation(conn, name=organisation, email=email, free_mail=cfg.free_mail_domains)
        org_id = found[0] if found else None
    hit = suppression_hit(conn, email=email, organisation_id=org_id)
    if hit:
        blockers.append(f"suppressed ({hit['scope'].lower()}: {hit['reason']})")
    if contact and contact["email_status"] in ("BOUNCED", "INVALID"):
        blockers.append(f"email status is {contact['email_status']}")
    if org_id:
        org = conn.execute("SELECT name, relationship_status FROM organisations WHERE organisation_id = ?",
                           (org_id,)).fetchone()
        if org["relationship_status"] in ("DO_NOT_CONTACT", "CLOSED"):
            blockers.append(f"organisation {org['name']} is {org['relationship_status']}")
        waiting = conn.execute(
            "SELECT COUNT(*) FROM replies WHERE organisation_id = ? AND response_status = 'AWAITING_RESPONSE' "
            "AND requires_response = 1", (org_id,)).fetchone()[0]
        if waiting:
            blockers.append(f"{waiting} reply from this organisation is awaiting Ritik's response — "
                            "answer it instead of sending automated outreach")
        other = conn.execute(
            "SELECT to_email, sent_at FROM outreach WHERE organisation_id = ? AND to_email <> ? AND status = 'SENT' "
            "AND touch_type = 'FIRST_TOUCH' AND sent_at >= datetime('now', '-30 days') ORDER BY sent_at DESC LIMIT 1",
            (org_id, email)).fetchone()
        if other:
            warnings.append(f"organisation already received a first touch at {other['to_email']} on {other['sent_at'][:10]}")
    dup = conn.execute(
        "SELECT outreach_id, status, COALESCE(sent_at, scheduled_for) AS at FROM outreach WHERE to_email = ? "
        "AND status IN ('SENT', 'SCHEDULED', 'APPROVED') AND COALESCE(sent_at, scheduled_for) >= datetime('now', ?) "
        "AND (? IS NULL OR COALESCE(subject, '') = ?) LIMIT 1",
        (email, f"-{recent_days} days", subject, subject or "")).fetchone()
    if dup:
        blockers.append(f"already {dup['status'].lower()} to this address on {dup['at'][:10]} (outreach {dup['outreach_id']})")
    if contact is None:
        warnings.append("recipient is not a known contact — it will be created when the send is recorded")
    elif contact["email_status"] in ("UNVERIFIED", "RISKY"):
        warnings.append(f"email is {contact['email_status'].lower()}")
    if email_domain(email) in cfg.free_mail_domains:
        warnings.append("free-mail address (gmail/yahoo/...): confirm it belongs to the organisation")
    return {"ok": not blockers, "to": email, "organisation_id": org_id, "blockers": blockers, "warnings": warnings}
