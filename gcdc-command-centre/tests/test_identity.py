import pytest

from gcdc.identity import (domain_from_url, email_domain, name_key, normalise_abn, normalise_email,
                           registrable_domain, split_emails)


@pytest.mark.parametrize("name", ["The Harbour Care Services Pty Ltd", "Harbour Care Services",
                                  "HARBOUR CARE SERVICES PTY. LTD."])
def test_name_key_ignores_legal_suffixes_and_case(name):
    assert name_key(name) == "harbour care services"


def test_abn_checksum():
    assert normalise_abn("51 824 753 556") == "51824753556"
    assert normalise_abn("51 824 753 557") is None  # bad checksum is not an identity
    assert normalise_abn("TBC") is None


def test_domains():
    assert domain_from_url("https://www.harbourcare.example.com.au/contact") == "example.com.au"
    assert registrable_domain("www.care.example.com.au") == "example.com.au"
    assert email_domain("Jane@HarbourCare.example") == "harbourcare.example"


def test_emails():
    assert normalise_email(" Jane@HarbourCare.example ") == "jane@harbourcare.example"
    assert split_emails("a@x.example; b@y.example / TBC") == ["a@x.example", "b@y.example"]
