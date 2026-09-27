"""Merge a duplicate organisation into the one to keep (all history is re-pointed)."""

from __future__ import annotations

import sqlite3

from gcdc.db import transaction
from gcdc.timeutil import now_iso

# (table, column) pairs that reference organisations.
_REFERENCES = [
    ("contacts", "organisation_id"), ("opportunities", "organisation_id"), ("outreach", "organisation_id"),
    ("follow_ups", "organisation_id"), ("replies", "organisation_id"), ("meetings", "organisation_id"),
    ("participant_referrals", "referring_organisation_id"), ("participants", "referring_organisation_id"),
    ("rosters", "organisation_id"), ("trial_shifts", "organisation_id"),
    ("revenue_transactions", "organisation_id"), ("worker_requirements", "organisation_id"),
    ("associate_provider_applications", "organisation_id"),
]
_FILL = ["legal_name", "abn", "website", "domain", "phone", "general_email", "suburb", "postcode", "state", "tier",
         "delivers_own_workers", "does_coordination", "ndis_registered", "notes"]


def merge_organisations(conn: sqlite3.Connection, *, keep_id: int, merge_id: int) -> dict:
    if keep_id == merge_id:
        raise SystemExit("keep and merge must be different organisations")
    keep = conn.execute("SELECT * FROM organisations WHERE organisation_id = ?", (keep_id,)).fetchone()
    dup = conn.execute("SELECT * FROM organisations WHERE organisation_id = ?", (merge_id,)).fetchone()
    if not keep or not dup:
        raise SystemExit("organisation not found")
    if dup["merged_into_id"]:
        raise SystemExit(f"organisation {merge_id} was already merged into {dup['merged_into_id']}")
    moved: dict[str, int] = {}
    now = now_iso()
    with transaction(conn):
        for table, column in _REFERENCES:
            moved[table] = conn.execute(f"UPDATE {table} SET {column} = ? WHERE {column} = ?",
                                        (keep_id, merge_id)).rowcount
        # Partnerships are unique per (organisation, type): fold duplicates into the kept one.
        for p in conn.execute("SELECT partnership_id, partnership_type FROM partnerships WHERE organisation_id = ?",
                              (merge_id,)).fetchall():
            existing = conn.execute("SELECT partnership_id FROM partnerships WHERE organisation_id = ? AND "
                                    "partnership_type = ?", (keep_id, p["partnership_type"])).fetchone()
            if existing:
                conn.execute("UPDATE associate_provider_applications SET partnership_id = ? WHERE partnership_id = ?",
                             (existing[0], p["partnership_id"]))
                conn.execute("INSERT OR IGNORE INTO partnership_documents (partnership_id, doc_code, status, submitted_at, "
                             "updated_at) SELECT ?, doc_code, status, submitted_at, updated_at FROM partnership_documents "
                             "WHERE partnership_id = ?", (existing[0], p["partnership_id"]))
                conn.execute("DELETE FROM partnership_documents WHERE partnership_id = ?", (p["partnership_id"],))
                conn.execute("DELETE FROM partnerships WHERE partnership_id = ?", (p["partnership_id"],))
            else:
                conn.execute("UPDATE partnerships SET organisation_id = ? WHERE partnership_id = ?",
                             (keep_id, p["partnership_id"]))
        conn.execute("DELETE FROM suppression WHERE scope = 'ORGANISATION' AND organisation_id = ? AND EXISTS "
                     "(SELECT 1 FROM suppression s WHERE s.scope = 'ORGANISATION' AND s.organisation_id = ?)",
                     (merge_id, keep_id))
        conn.execute("UPDATE suppression SET organisation_id = ? WHERE organisation_id = ?", (keep_id, merge_id))
        conn.execute("UPDATE organisation_identifiers SET organisation_id = ? WHERE organisation_id = ?",
                     (keep_id, merge_id))
        fills = {c: dup[c] for c in _FILL if keep[c] is None and dup[c] is not None}
        if fills:
            conn.execute(f"UPDATE organisations SET {', '.join(f'{c} = ?' for c in fills)}, updated_at = ? "
                         f"WHERE organisation_id = ?", (*fills.values(), now, keep_id))
        conn.execute("UPDATE organisations SET first_seen_at = MIN(first_seen_at, ?) WHERE organisation_id = ?",
                     (dup["first_seen_at"], keep_id))
        conn.execute("UPDATE organisations SET merged_into_id = ?, updated_at = ? WHERE organisation_id = ?",
                     (keep_id, now, merge_id))
    return {"kept": keep_id, "merged": merge_id, "moved": {k: v for k, v in moved.items() if v},
            "filled_fields": list(fills)}
