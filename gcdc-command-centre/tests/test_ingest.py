"""The ingest layer is the only way agents write: one organisation per real business, idempotent, isolated."""

from conftest import one

HARBOUR = {"name": "Harbour Care Services Pty Ltd", "website": "https://www.harbourcare.example/contact"}


def orgs(conn):
    return one(conn, "SELECT COUNT(*) FROM organisations WHERE merged_into_id IS NULL")


def test_same_business_from_different_agents_is_one_organisation(conn, ingest):
    ingest([{"type": "organisation", **HARBOUR, "region": "Gold Coast"},
            {"type": "opportunity", "organisation": HARBOUR, "opportunity_type_code": "SUPPORT_COORDINATION",
             "source_code": "GOOGLE", "source_key": "disc-1"}], agent="OPPORTUNITY_DISCOVERY")
    # Outreach agent only knows a contact email and a slightly different name.
    r = ingest([{"type": "outreach", "organisation": {"name": "Harbour Care Services"},
                 "to_email": "Jane@HarbourCare.example", "subject": "Hello", "status": "SENT",
                 "sent_at": "2026-09-01T00:00:00Z", "touch_type": "FIRST_TOUCH", "external_message_id": "<m1@x>"}],
               agent="OUTREACH")
    assert r.failed == 0, r.errors
    # Inbox agent sees a reply from another person at the same domain.
    ingest([{"type": "reply", "from_email": "intake@harbourcare.example", "received_at": "2026-09-02T00:00:00Z",
             "classification": "POSITIVE", "external_message_id": "<r1@x>"}], agent="INBOX")
    assert orgs(conn) == 1
    assert one(conn, "SELECT COUNT(*) FROM opportunities") == 1


def test_same_name_different_abn_stays_separate(conn, ingest):
    ingest([{"type": "organisation", "name": "Kookaburra Care", "abn": "51 824 753 556"},
            {"type": "organisation", "name": "Kookaburra Care", "abn": "98 999 999 999"}])
    assert orgs(conn) == 2


def test_free_mail_domain_is_not_an_organisation_identity(conn, ingest):
    ingest([{"type": "organisation", "name": "Sole Trader A", "general_email": "a.person@gmail.com"},
            {"type": "organisation", "name": "Sole Trader B", "general_email": "b.person@gmail.com"}])
    assert orgs(conn) == 2


def test_reingesting_a_batch_is_idempotent(conn, ingest):
    batch = [{"type": "organisation", **HARBOUR},
             {"type": "opportunity", "organisation": HARBOUR, "opportunity_type_code": "SUBCONTRACTING",
              "source_code": "SEEK", "source_key": "seek-123"},
             {"type": "outreach", "organisation": HARBOUR, "to_email": "info@harbourcare.example", "status": "SENT",
              "sent_at": "2026-09-01T00:00:00Z", "external_message_id": "<m2@x>"},
             {"type": "reply", "from_email": "info@harbourcare.example", "received_at": "2026-09-03T00:00:00Z",
              "external_message_id": "<r2@x>", "classification": "NEUTRAL"}]
    ingest(batch)
    counts = [one(conn, f"SELECT COUNT(*) FROM {t}") for t in ("organisations", "opportunities", "outreach", "replies")]
    again = ingest(batch)
    assert again.failed == 0
    assert [one(conn, f"SELECT COUNT(*) FROM {t}") for t in ("organisations", "opportunities", "outreach",
                                                              "replies")] == counts


def test_bad_record_is_rejected_without_losing_the_batch(conn, ingest):
    r = ingest([{"type": "organisation", "name": "Good Org"},
                {"type": "opportunity", "organisation": {"name": "Good Org"}, "priority": "P9",
                 "source_code": "GOOGLE"},
                {"type": "no_such_type"},
                {"type": "organisation", "name": "Second Good Org"}])
    assert (r.succeeded, r.failed) == (2, 2)
    assert orgs(conn) == 2
    assert one(conn, "SELECT COUNT(*) FROM opportunities") == 0


def test_suppressed_address_blocks_outreach(conn, ingest):
    assert ingest([{"type": "suppression", "email": "stop@harbourcare.example", "reason": "UNSUBSCRIBE"}]).failed == 0
    r = ingest([{"type": "outreach", "organisation": HARBOUR, "to_email": "stop@harbourcare.example",
                 "status": "SCHEDULED", "scheduled_for": "2026-10-01T00:00:00Z"}])
    assert r.failed == 1 and "suppress" in r.errors[0]["error"].lower()
    assert one(conn, "SELECT COUNT(*) FROM outreach") == 0
    # An invalid reason is rejected rather than stored as something else.
    bad = ingest([{"type": "suppression", "email": "x@harbourcare.example", "reason": "UNSUBSCRIBED"}])
    assert bad.failed == 1


def test_placeholder_values_are_unknown_not_data(conn, ingest):
    r = ingest([{"type": "contact", "organisation": HARBOUR, "email": "TBC", "full_name": "Not publicly verified"}])
    assert one(conn, "SELECT COUNT(*) FROM contacts WHERE email = 'TBC' OR full_name LIKE 'Not publicly%'") == 0
    assert r.failed in (0, 1)


def test_stage_follows_evidence_and_never_moves_backwards(conn, ingest):
    ingest([{"type": "opportunity", "organisation": HARBOUR, "opportunity_type_code": "SUBCONTRACTING",
             "source_code": "GOOGLE", "source_key": "o1"},
            {"type": "outreach", "organisation": HARBOUR, "to_email": "info@harbourcare.example", "status": "SENT",
             "sent_at": "2026-09-01T00:00:00Z", "external_message_id": "<m3@x>"},
            {"type": "reply", "from_email": "info@harbourcare.example", "received_at": "2026-09-02T00:00:00Z",
             "external_message_id": "<r3@x>", "classification": "POSITIVE"}])
    stage = lambda: one(conn, "SELECT stage_code FROM pbi_fact_opportunity")  # noqa: E731
    assert stage() == "POSITIVE"
    # A late discovery run re-reports the lead as FOUND: the stage must not regress.
    ingest([{"type": "opportunity", "organisation": HARBOUR, "opportunity_type_code": "SUBCONTRACTING",
             "source_code": "GOOGLE", "source_key": "o1", "stage_code": "FOUND"}])
    assert stage() == "POSITIVE"
