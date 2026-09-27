"""Shared fixtures: every test gets its own throwaway database and export folder."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from gcdc.config import load_config  # noqa: E402
from gcdc.db import open_db, transaction  # noqa: E402
from gcdc.ingest import IngestContext, ingest_records  # noqa: E402


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("GCDC_DB", str(tmp_path / "gcdc.db"))
    monkeypatch.setenv("GCDC_EXPORT_DIR", str(tmp_path / "exports"))
    monkeypatch.setenv("GCDC_BRIEFS_DIR", str(tmp_path / "briefs"))
    return load_config(ROOT / "config" / "gcdc.toml")


@pytest.fixture
def conn(cfg):
    c = open_db(cfg)
    yield c
    c.close()


@pytest.fixture
def ingest(conn, cfg):
    """ingest(records, agent=...) -> BatchResult, committed like an agent batch."""

    def _ingest(records, *, agent="TEST", system=None):
        ctx = IngestContext(conn, cfg, agent_code=agent, source_system=system)
        with transaction(conn):
            return ingest_records(ctx, records)

    return _ingest


@pytest.fixture
def demo(conn, cfg):
    from gcdc.demo import seed_demo

    result = seed_demo(conn, cfg)
    assert result["failed"] == 0, result["errors"]
    return conn


def one(conn, sql, *params):
    return conn.execute(sql, params).fetchone()[0]
