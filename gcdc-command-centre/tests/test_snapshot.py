"""daily_metrics_snapshot is append-only history."""

import sqlite3
from datetime import timedelta

import pytest
from conftest import one

from gcdc.snapshot import snapshot_catch_up, take_snapshot
from gcdc.timeutil import local_today


def test_snapshot_is_written_once_and_never_overwritten(demo, cfg):
    day = local_today(cfg.utc_offset_hours) - timedelta(days=1)
    assert take_snapshot(demo, day, offset_hours=cfg.utc_offset_hours) is True
    first = dict(demo.execute("SELECT * FROM daily_metrics_snapshot WHERE snapshot_date = ?", (day.isoformat(),))
                 .fetchone())
    demo.execute("UPDATE rosters SET weekly_hours = 99")  # business changes after the fact...
    assert take_snapshot(demo, day, offset_hours=cfg.utc_offset_hours) is False  # ...history is not rewritten
    again = dict(demo.execute("SELECT * FROM daily_metrics_snapshot WHERE snapshot_date = ?", (day.isoformat(),))
                 .fetchone())
    assert again == first


def test_snapshot_rows_cannot_be_updated_or_deleted(demo, cfg):
    day = local_today(cfg.utc_offset_hours) - timedelta(days=1)
    take_snapshot(demo, day, offset_hours=cfg.utc_offset_hours)
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        demo.execute("UPDATE daily_metrics_snapshot SET mrr = 1")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        demo.execute("DELETE FROM daily_metrics_snapshot")


def test_catch_up_fills_gaps_without_duplicates(demo, cfg):
    off = cfg.utc_offset_hours
    today = local_today(off)
    take_snapshot(demo, today - timedelta(days=5), offset_hours=off)
    written = snapshot_catch_up(demo, offset_hours=off)
    assert written == [(today - timedelta(days=d)).isoformat() for d in (4, 3, 2, 1)]
    assert snapshot_catch_up(demo, offset_hours=off) == []
    assert one(demo, "SELECT COUNT(*) FROM daily_metrics_snapshot WHERE is_catch_up = 1") == 3


def test_future_snapshot_refused(demo, cfg):
    with pytest.raises(ValueError):
        take_snapshot(demo, local_today(cfg.utc_offset_hours) + timedelta(days=1), offset_hours=cfg.utc_offset_hours)
