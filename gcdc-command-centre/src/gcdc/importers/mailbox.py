"""Turn Microsoft Graph mail messages into outreach / reply / bounce records.

Deterministic rules only (no AI):
  * Sent Items -> outreach. First message in a conversation = FIRST_TOUCH;
    later ones = FOLLOW_UP, or REPLY when the contact had already written in.
  * Non-delivery reports -> bounce (hard unless the text says temporary).
  * "Automatic reply" -> reply classified OUT_OF_OFFICE.
  * Any other inbound message from a known contact / organisation domain, or on
    a known outreach thread -> reply, UNCLASSIFIED and awaiting response. The
    Inbox agent (AI) classifies it later via `gcdc ingest`.
  * Everything else (newsletters, job alerts, applicants) is ignored.
A sent message that matches an outreach imported from a planning sheet (same
recipient, within 3 days, no message id yet) confirms that record instead of
creating a duplicate.
"""

from __future__ import annotations

import re
import sqlite3
from typing import Any

from gcdc.identity import email_domain, normalise_email

_NDR_SENDER = re.compile(r"(microsoftexchange|postmaster|mailer-daemon|mail delivery subsystem)", re.IGNORECASE)
_NDR_SUBJECT = re.compile(r"^(undeliverable|undelivered|delivery status notification|mail delivery failed|"
                          r"returned mail|failure notice)", re.IGNORECASE)
_AUTO_SUBJECT = re.compile(r"^(automatic reply|auto(matic)?[- ]?reply|out of (the )?office)", re.IGNORECASE)
_SOFT_BOUNCE = re.compile(r"(mailbox (is )?full|quota|temporar|try again later|despite repeated attempts)",
                          re.IGNORECASE)
_REPLY_PREFIX = re.compile(r"^\s*(re|aw|sv)\s*:", re.IGNORECASE)
_EMAIL = re.compile(r"[A-Za-z0-9._%+'-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


def _addr(obj: dict | None) -> str | None:
    if not obj:
        return None
    return normalise_email((obj.get("emailAddress") or {}).get("address"))


def _when(msg: dict, sent: bool) -> str:
    s, r = msg.get("sentDateTime"), msg.get("receivedDateTime")
    if sent:  # Sent Items: receivedDateTime is when a delayed/scheduled send actually left
        return max(x for x in (s, r) if x)
    return r or s


def mailbox_records(conn: sqlite3.Connection, messages: list[dict[str, Any]], *, owner: str,
                    free_mail: frozenset[str] = frozenset()) -> tuple[list[dict], int]:
    owner = normalise_email(owner)
    own_domain = email_domain(owner)
    msgs = sorted(messages, key=lambda m: m.get("receivedDateTime") or m.get("sentDateTime") or "")
    known_threads = {r[0] for r in conn.execute("SELECT DISTINCT thread_id FROM outreach WHERE thread_id IS NOT NULL")}
    known_emails = {r[0] for r in conn.execute("SELECT email FROM contacts WHERE email IS NOT NULL")}
    known_domains = {r[0] for r in conn.execute(
        "SELECT identifier_value FROM organisation_identifiers WHERE identifier_type = 'DOMAIN'")}
    seen_out: dict[str, int] = {}      # conversation -> outbound messages so far
    seen_in: dict[str, bool] = {}      # conversation -> contact has written in
    records: list[dict] = []
    ignored = 0
    for m in msgs:
        sender = _addr(m.get("from") or m.get("sender"))
        subject = m.get("subject") or ""
        conv = m.get("conversationId")
        mid = (m.get("internetMessageId") or m.get("id") or "").strip() or None
        if m.get("isDraft"):
            continue
        if sender == owner:
            to = next((a for a in (_addr(r) for r in m.get("toRecipients") or []) if a), None)
            if not to or email_domain(to) == own_domain:
                ignored += 1
                continue
            n = seen_out.get(conv, 0)
            touch = "REPLY" if seen_in.get(conv) else ("FOLLOW_UP" if n or _REPLY_PREFIX.match(subject) else "FIRST_TOUCH")
            seen_out[conv] = n + 1
            when = _when(m, sent=True)
            rec = {"type": "outreach", "to_email": to, "subject": subject[:300], "status": "SENT", "sent_at": when,
                   "touch_type": touch, "external_message_id": mid, "thread_id": conv, "evidence": "MAILBOX",
                   "channel": "EMAIL"}
            planned = conn.execute(
                "SELECT outreach_id FROM outreach WHERE to_email = ? AND external_message_id IS NULL "
                "AND status IN ('SENT', 'SCHEDULED', 'APPROVED') AND evidence IN ('SHEET_PLAN', 'AGENT_LOG', 'MANUAL') "
                "AND abs(julianday(COALESCE(sent_at, scheduled_for)) - julianday(?)) <= 3 "
                "ORDER BY abs(julianday(COALESCE(sent_at, scheduled_for)) - julianday(?)) LIMIT 1",
                (to, when, when)).fetchone()
            if planned:
                rec["outreach_id"] = planned[0]
            elif to not in known_emails and email_domain(to) not in known_domains:
                # Unknown recipient: name the organisation after its domain (or the address for free-mail).
                dom = email_domain(to)
                rec["organisation"] = {"name": to if (not dom or dom in free_mail) else dom, "general_email": to}
            records.append(rec)
            if conv:
                known_threads.add(conv)
            continue
        preview = f"{subject}\n{m.get('bodyPreview') or ''}\n{(m.get('body') or {}).get('content') or ''}"
        if (sender and _NDR_SENDER.search(sender)) or _NDR_SUBJECT.match(subject):
            failed = [e.lower() for e in _EMAIL.findall(preview)
                      if email_domain(e) != own_domain and not _NDR_SENDER.search(e)]
            for addr in dict.fromkeys(failed):
                records.append({"type": "bounce", "to_email": addr, "bounced_at": _when(m, sent=False),
                                "bounce_type": "SOFT" if _SOFT_BOUNCE.search(preview) else "HARD",
                                "bounce_reason": re.sub(r"\s+", " ", (m.get("bodyPreview") or subject))[:300]})
            if not failed:
                ignored += 1
            continue
        related = (conv in known_threads) or (sender in known_emails) or (email_domain(sender) in known_domains)
        if not sender or not related:
            ignored += 1
            continue
        seen_in[conv] = True
        auto = bool(_AUTO_SUBJECT.match(subject))
        records.append({"type": "reply", "from_email": sender, "subject": subject[:300],
                        "received_at": _when(m, sent=False), "external_message_id": mid, "thread_id": conv,
                        "classification": "OUT_OF_OFFICE" if auto else "UNCLASSIFIED",
                        "summary": (m.get("bodyPreview") or "")[:500] or None, "classified_by": "mailbox rules"})
    return records, ignored
