from datetime import date

import pytest

from gcdc.importers.common import safe_date
from gcdc.timeutil import parse_date


@pytest.mark.parametrize("text, expected", [
    ("2026-09-14", date(2026, 9, 14)),
    ("14/9/2026", date(2026, 9, 14)),
    ("Monday 14/9/26", date(2026, 9, 14)),
    ("Friday 28//8/2026", date(2026, 8, 28)),  # doubled separator: unambiguous, accepted
    ("Mon 14 Sep 2026", date(2026, 9, 14)),
])
def test_parses_gcdc_date_formats(text, expected):
    assert parse_date(text) == expected


def test_unknown_or_ambiguous_dates_are_reported_not_guessed():
    with pytest.raises(ValueError):
        parse_date("wednesday")
    with pytest.raises(ValueError):
        parse_date("14 Sep")  # no year and none supplied
    d, problem = safe_date("22/9/2022", not_before=date(2026, 1, 1))  # likely a typo for 2026, but not assumed
    assert d is None and "before records start" in problem
