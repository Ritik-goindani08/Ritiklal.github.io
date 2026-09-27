"""Importers: canonical CSV templates, the existing Google Sheets, mailbox messages, legacy agent databases."""

import csv
import json
import re
import sqlite3
from pathlib import Path

import pytest
from conftest import one

from gcdc.importers.legacy import audit_legacy, import_legacy
from gcdc.importers.runner import import_canonical, import_file, import_mailbox_json
from gcdc.reconcile import reconcile

TEMPLATES = Path(__file__).resolve().parents[1] / "templates"
ORDER = ["worker", "worker_availability", "participant", "roster", "revenue", "referral", "meeting", "trial_shift",
         "partnership", "application", "suppression", "compliance_document", "worker_requirement"]


def test_every_template_is_covered():
    assert sorted(p.stem for p in TEMPLATES.glob("*.csv")) == sorted(ORDER)


def test_template_example_rows_are_skipped(conn, cfg):
    for entity in ORDER:
        r = import_canonical(conn, cfg, TEMPLATES / f"{entity}.csv", entity)
        assert r["processed"] == 0, entity


def test_template_headers_are_valid_fields(conn, cfg, tmp_path):
    """Turn each EXAMPLE row into a real row and import it: every column must be accepted."""
    for entity in ORDER:
        rows = list(csv.reader(open(TEMPLATES / f"{entity}.csv", encoding="utf-8")))
        real = [[re.sub("(?i)example", "Test", c) for c in row] for row in rows]
        f = tmp_path / f"{entity}.csv"
        with open(f, "w", newline="", encoding="utf-8") as fh:
            csv.writer(fh).writerows(real)
        r = import_canonical(conn, cfg, f, entity)
        assert (r["processed"], r["failed"]) == (1, 0), (entity, r["errors"])
    k = dict(conn.execute("SELECT mrr, active_participants, workers_active FROM pbi_kpi_current").fetchone())
    assert k["mrr"] == pytest.approx(8 * 70.23 * 4.333333, abs=0.01)
    assert k["active_participants"] == 1 and k["workers_active"] == 1


CRM_HEADER = ["Rank", "Company", "Type", "Suburb", "P/code", "Region", "Tier", "Phone", "Email", "ABN", "Website",
              "Priority", "Score", "Status", "Last contact", "Next follow-up", "Owner", "Notes", "Source"]


def _crm(tmp_path, rows):
    f = tmp_path / "GCDC_Referral_CRM.csv"
    with open(f, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(CRM_HEADER)
        w.writerows(rows)
    return f


def test_referral_crm_import_is_evidence_based_and_idempotent(conn, cfg, tmp_path):
    f = _crm(tmp_path, [
        [1, "Wattle Test Coordination", "Support Coordinator", "Robina", "4226", "Gold Coast", 1, "",
         "intake@wattle.example", "", "wattle.example", "Call first", 90, "Warm - interested", "2026-09-10",
         "2026-09-24", "Ritik", "Wants capability statement", "Original"],
        [2, "Banksia Test Care", "Provider", "Logan", "4114", "Logan", 2, "", "hello@banksia.example", "", "", "High", 70,
         "Trial booked", "", "", "Ritik", "", "Original"],  # status without a date: reported, not invented
        [3, "Old Test Care", "Provider", "Logan", "4114", "Logan", 3, "", "", "", "", "", 10, "Left voicemail",
         "2025-03-01", "", "", "", ""],  # before records_start_date
    ])
    first = import_file(conn, cfg, "referral-crm", f)
    assert first["failed"] == 0, first["errors"]
    assert one(conn, "SELECT COUNT(*) FROM organisations") == 3
    assert one(conn, "SELECT COUNT(*) FROM outreach") == 1  # only the dated, in-range touch
    assert one(conn, "SELECT classification FROM replies") == "POSITIVE"
    assert one(conn, "SELECT COUNT(*) FROM trial_shifts") == 0  # a "Trial booked" label is not a trial record
    assert one(conn, "SELECT action_type FROM follow_ups") == "CALL"
    assert first["problem_count"] == 2
    again = import_file(conn, cfg, "referral-crm", f)
    assert again["failed"] == 0
    assert [one(conn, f"SELECT COUNT(*) FROM {t}") for t in ("organisations", "opportunities", "outreach", "replies",
                                                              "follow_ups")] == [3, 3, 1, 1, 1]


def _msg(mid, conv, frm, to, subject, when, preview=""):
    return {"id": mid, "internetMessageId": f"<{mid}@x>", "conversationId": conv, "subject": subject,
            "from": {"emailAddress": {"address": frm}}, "toRecipients": [{"emailAddress": {"address": to}}] if to else [],
            "sentDateTime": when, "receivedDateTime": when, "bodyPreview": preview, "isDraft": False}


def test_mailbox_rules(conn, cfg, tmp_path):
    me = "info@gcdc.example"
    messages = [
        _msg("s1", "c1", me, "intake@harbour.example", "Support workers available", "2026-09-01T00:00:00Z"),
        _msg("r1", "c1", "jane@harbour.example", me, "RE: Support workers available", "2026-09-02T00:00:00Z"),
        _msg("s2", "c1", me, "jane@harbour.example", "RE: Support workers available", "2026-09-02T02:00:00Z"),
        _msg("s3", "c2", me, "wrong@nowhere.example", "Support workers available", "2026-09-03T00:00:00Z"),
        _msg("b1", "c2", "postmaster@gcdc.example", me, "Undeliverable: Support workers available",
             "2026-09-03T00:01:00Z", "Delivery has failed to these recipients: wrong@nowhere.example"),
        _msg("n1", "c3", "news@newsletter.example", me, "Our September newsletter", "2026-09-04T00:00:00Z"),
        _msg("a1", "c1", "jane@harbour.example", me, "Automatic reply: Support workers available", "2026-09-05T00:00:00Z"),
    ]
    f = tmp_path / "mail.json"
    f.write_text(json.dumps({"mailbox": me, "messages": messages}))
    r = import_mailbox_json(conn, cfg, f)
    assert r["failed"] == 0, r["errors"]
    assert r["ignored_unrelated"] == 1  # the newsletter
    touches = dict(conn.execute("SELECT external_message_id, touch_type FROM outreach").fetchall())
    assert touches == {"<s1@x>": "FIRST_TOUCH", "<s2@x>": "REPLY", "<s3@x>": "FIRST_TOUCH"}
    assert one(conn, "SELECT status FROM outreach WHERE external_message_id = '<s3@x>'") == "BOUNCED"
    assert one(conn, "SELECT COUNT(*) FROM replies WHERE classification = 'OUT_OF_OFFICE'") == 1
    # The reply we answered is marked responded by our REPLY touch (reconcile runs in every refresh).
    reconcile(conn)
    assert one(conn, "SELECT response_status FROM replies WHERE external_message_id = '<r1@x>'") == "RESPONDED"
    again = import_mailbox_json(conn, cfg, f)
    assert again["failed"] == 0 and one(conn, "SELECT COUNT(*) FROM outreach") == 3


def test_legacy_agent_database_adoption(conn, cfg, tmp_path):
    legacy = tmp_path / "outreach_agent.db"
    lc = sqlite3.connect(legacy)
    lc.executescript("""
        CREATE TABLE leads (id INTEGER PRIMARY KEY, company TEXT, website TEXT, region TEXT);
        CREATE TABLE sent_emails (id INTEGER PRIMARY KEY, company TEXT, to_email TEXT, subject TEXT, sent_at TEXT,
                                  message_id TEXT);
        INSERT INTO leads VALUES (1, 'Seabreeze Care', 'https://seabreeze.example', 'Gold Coast');
        INSERT INTO sent_emails VALUES (10, 'Seabreeze Care', 'intake@seabreeze.example', 'Hello', '2026-09-01T00:00:00Z',
                                        '<legacy-10@x>');
    """)
    lc.commit()
    lc.close()
    audit = audit_legacy(legacy)
    assert {t["table"]: t["suggested_record_type"] for t in audit["tables"]} == {
        "leads": "organisation", "sent_emails": "outreach"}
    mapping = tmp_path / "map.toml"
    mapping.write_text("""
[[tables]]
source = "leads"
record_type = "organisation"
source_key_column = "id"
[tables.columns]
company = "name"
website = "website"
region = "region"

[[tables]]
source = "sent_emails"
record_type = "outreach"
source_key_column = "id"
[tables.columns]
company = "organisation.name"
to_email = "to_email"
subject = "subject"
sent_at = "sent_at"
message_id = "external_message_id"
[tables.constants]
status = "SENT"
""")
    for _ in range(2):  # idempotent
        r = import_legacy(conn, cfg, legacy, mapping)
        assert all(t["failed"] == 0 for t in r["tables"]), r
    assert one(conn, "SELECT COUNT(*) FROM organisations") == 1
    assert one(conn, "SELECT COUNT(*) FROM outreach") == 1
