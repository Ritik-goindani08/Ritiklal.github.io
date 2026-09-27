"""Organisation identity resolution — the rule that prevents duplicates.

Every agent that finds an organisation goes through `resolve_organisation`.
Match order (first hit wins):
    1. EXTERNAL id already mapped (e.g. 'sah_db:SAH-012')
    2. ABN
    3. Website / email domain (never free-mail domains like gmail.com)
    4. Exact general email address
    5. Normalised name key
A candidate is rejected when both sides have an ABN and they differ (e.g. two
franchisees sharing one brand website are different businesses).
"""

from __future__ import annotations

import re
import sqlite3
import unicodedata
from dataclasses import dataclass
from urllib.parse import urlparse

from gcdc.timeutil import now_iso

_LEGAL_SUFFIXES = re.compile(
    r"\b(pty\.?\s*ltd\.?|pty\.?\s*limited|proprietary\s+limited|ltd\.?|limited|inc\.?|incorporated|"
    r"co\.?|corporation|corp\.?|p/l|pl)\s*$")
_THE_TRUSTEE = re.compile(r"^the trustee for\s+")
_BRACKETS = re.compile(r"\([^)]*\)|\[[^\]]*\]")
_NON_ALNUM = re.compile(r"[^a-z0-9]+")

_AU_SLDS = {"com.au", "org.au", "net.au", "asn.au", "edu.au", "id.au", "csiro.au", "com", "org", "net"}
_ROLE_LOCALPARTS = {
    "info", "admin", "administration", "hello", "enquiries", "enquiry", "contact", "office", "reception",
    "support", "intake", "referrals", "referral", "team", "mail", "care", "service", "services", "accounts",
    "bookings", "suppliers", "procurement", "businessdevelopment", "bd", "operations", "roster", "rostering",
}


def name_key(name: str) -> str:
    text = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode().lower()
    text = _BRACKETS.sub(" ", text)
    text = text.replace("&", " and ").replace("+", " and ")
    text = _THE_TRUSTEE.sub("", text.strip())
    text = re.sub(r"\s+", " ", text).strip(" .,-")
    for _ in range(2):  # 'Foo Pty Ltd Inc' style stacking
        text = _LEGAL_SUFFIXES.sub("", text).strip(" .,-")
    text = re.sub(r"^the\s+", "", text)
    return _NON_ALNUM.sub(" ", text).strip()


def normalise_abn(value) -> str | None:
    """Return an 11-digit ABN if it passes the ATO checksum, else None."""
    if value is None:
        return None
    if isinstance(value, float):
        value = f"{value:.0f}"
    digits = re.sub(r"\D", "", str(value))
    if len(digits) != 11:
        return None
    weights = (10, 1, 3, 5, 7, 9, 11, 13, 15, 17, 19)
    nums = [int(d) for d in digits]
    nums[0] -= 1
    return digits if sum(n * w for n, w in zip(nums, weights)) % 89 == 0 else None


def domain_from_url(url: str | None) -> str | None:
    if not url:
        return None
    text = str(url).strip().lower()
    if not text or " " in text.strip():
        text = text.split()[0] if text.split() else ""
    if "://" not in text:
        text = "http://" + text
    host = urlparse(text).hostname or ""
    return registrable_domain(host)


def registrable_domain(host: str | None) -> str | None:
    host = (host or "").strip(".").lower()
    if host.startswith("www."):
        host = host[4:]
    if not host or "." not in host:
        return None
    labels = host.split(".")
    if host.endswith(".gov.au"):
        keep = 4
    elif len(labels) >= 3 and ".".join(labels[-2:]) in _AU_SLDS and labels[-1] == "au":
        keep = 3
    else:
        keep = 2
    return ".".join(labels[-keep:])


def normalise_email(value) -> str | None:
    if value is None:
        return None
    text = str(value).strip().lower().strip("<>;, ")
    if text.startswith("mailto:"):
        text = text[7:]
    return text if re.fullmatch(r"[^@\s]+@[^@\s]+\.[a-z]{2,}", text) else None


def split_emails(value) -> list[str]:
    """Cells sometimes hold 'a@x.com, b@x.com' or 'a@x.com | also: b@x.com'."""
    if value is None:
        return []
    found = re.findall(r"[A-Za-z0-9._%+'-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", str(value))
    out = []
    for e in found:
        n = normalise_email(e)
        if n and n not in out:
            out.append(n)
    return out


def email_domain(email: str | None) -> str | None:
    email = normalise_email(email)
    return registrable_domain(email.split("@", 1)[1]) if email else None


def is_role_address(email: str | None) -> bool | None:
    email = normalise_email(email)
    if not email:
        return None
    local = re.sub(r"[^a-z]", "", email.split("@", 1)[0])
    return local in _ROLE_LOCALPARTS


@dataclass
class OrgMatch:
    organisation_id: int
    created: bool
    matched_on: str | None


def _identifiers_for(*, name, abn, website, email, external, free_mail) -> list[tuple[str, str]]:
    ids: list[tuple[str, str]] = []
    if external:
        ids.append(("EXTERNAL", external))
    if abn:
        ids.append(("ABN", abn))
    dom = domain_from_url(website)
    if dom and dom not in free_mail:
        ids.append(("DOMAIN", dom))
    edom = email_domain(email)
    if edom and edom not in free_mail and ("DOMAIN", edom) not in ids:
        ids.append(("DOMAIN", edom))
    if email:
        ids.append(("EMAIL", email))
    if name:
        key = name_key(name)
        if key:
            ids.append(("NAME_KEY", key))
    return ids


def find_organisation(conn: sqlite3.Connection, *, name=None, abn=None, website=None, email=None,
                      external=None, free_mail=frozenset()) -> tuple[int, str] | None:
    abn = normalise_abn(abn)
    email = normalise_email(email)
    for id_type, value in _identifiers_for(name=name, abn=abn, website=website, email=email,
                                           external=external, free_mail=free_mail):
        row = conn.execute(
            "SELECT i.organisation_id, o.abn, o.merged_into_id FROM organisation_identifiers i "
            "JOIN organisations o ON o.organisation_id = i.organisation_id "
            "WHERE i.identifier_type = ? AND i.identifier_value = ?", (id_type, value)).fetchone()
        if not row:
            continue
        org_id, org_abn, merged_into = row
        while merged_into:  # follow merges to the surviving record
            org_id, org_abn, merged_into = conn.execute(
                "SELECT organisation_id, abn, merged_into_id FROM organisations WHERE organisation_id = ?",
                (merged_into,)).fetchone()
        if id_type not in ("EXTERNAL", "ABN") and abn and org_abn and abn != org_abn:
            continue  # same brand/name, different legal entity
        return org_id, id_type
    return None


def register_identifiers(conn: sqlite3.Connection, organisation_id: int, *, name=None, abn=None, website=None,
                         email=None, external=None, source=None, free_mail=frozenset()) -> None:
    now = now_iso()
    for id_type, value in _identifiers_for(name=name, abn=normalise_abn(abn), website=website,
                                           email=normalise_email(email), external=external, free_mail=free_mail):
        conn.execute(
            "INSERT OR IGNORE INTO organisation_identifiers (identifier_type, identifier_value, organisation_id, "
            "source, created_at) VALUES (?, ?, ?, ?, ?)", (id_type, value, organisation_id, source, now))
