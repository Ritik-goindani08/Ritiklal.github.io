"""Daily BI Agent: deterministic brief, business-day rules, storage, and the constrained optional AI step."""

import json
import sys
import types
from datetime import date

import pytest
from conftest import one

from gcdc.brief import focus_candidates, gather_facts, run_brief
from gcdc.timeutil import local_today

SECTIONS = ["## Recommended focus", "## Revenue (actual)", "## Pipeline (estimated — not revenue)",
            "## Urgent actions", "## Outreach", "## Risks"]


def test_brief_on_demo_data(demo, cfg):
    r = run_brief(demo, cfg, force=True)
    md = r["markdown"]
    for s in SECTIONS:
        assert s in md
    assert 1 <= len(r["focus"]) <= 3
    k = dict(demo.execute("SELECT mrr, revenue_target FROM pbi_kpi_current").fetchone())
    assert f"MRR **${k['mrr']:,.0f}** of ${k['revenue_target']:,.0f}" in md  # same numbers as the dashboard
    assert one(demo, "SELECT COUNT(*) FROM pbi_daily_brief") == 1
    assert (cfg.briefs_dir / f"{r['brief_date']}.md").read_text(encoding="utf-8") == md
    run = demo.execute("SELECT status, records_created FROM agent_runs WHERE agent_code = 'DAILY_BI'").fetchone()
    assert tuple(run) == ("SUCCESS", 1)


def test_focus_is_ranked_closest_to_revenue(demo, cfg):
    facts = gather_facts(demo, cfg, local_today(cfg.utc_offset_hours))
    ids = [c.id for c in focus_candidates(facts, cfg)]
    # The demo has an open referral, an unconverted trial and an unapproved roster rate.
    assert ids[0] == "referral"
    assert ids.index("trial") < ids.index("followups")
    assert ids.index("rate") < ids.index("followups")


def test_rerun_same_day_replaces_the_brief(demo, cfg):
    run_brief(demo, cfg, force=True)
    run_brief(demo, cfg, force=True)
    assert one(demo, "SELECT COUNT(*) FROM daily_briefs") == 1


@pytest.mark.parametrize("day", [date(2026, 9, 26), date(2026, 10, 5)])  # Saturday; QLD King's Birthday
def test_skips_non_business_days(conn, cfg, day):
    r = run_brief(conn, cfg, brief_date=day)
    assert r["skipped"] is True
    assert one(conn, "SELECT COUNT(*) FROM agent_runs WHERE agent_code = 'DAILY_BI'") == 0


def test_empty_database_still_produces_a_brief(conn, cfg):
    r = run_brief(conn, cfg, force=True, save=False)
    assert "MRR **$0**" in r["markdown"]


class _FakeMessages:
    def __init__(self, reply):
        self.reply, self.calls = reply, []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if isinstance(self.reply, Exception):
            raise self.reply
        block = types.SimpleNamespace(type="text", text=json.dumps(self.reply))
        return types.SimpleNamespace(stop_reason="end_turn", content=[block], model="fake-model")


@pytest.fixture
def fake_claude(monkeypatch):
    def install(reply):
        messages = _FakeMessages(reply)
        client = types.SimpleNamespace(beta=types.SimpleNamespace(messages=messages))
        monkeypatch.setitem(sys.modules, "anthropic", types.SimpleNamespace(Anthropic=lambda: client))
        return messages

    return install


def test_ai_step_only_chooses_among_computed_candidates(demo, cfg, fake_claude):
    fake = fake_claude({"choices": [{"candidate_id": "rate", "why_today": "Rate approval adds MRR today."},
                                    {"candidate_id": "invented", "why_today": "Not a real candidate."}]})
    r = run_brief(demo, cfg, force=True, use_ai=True)
    assert [f["id"] for f in r["focus"]] == ["rate"]
    assert "Rate approval adds MRR today." in r["markdown"]
    assert r["generator"].startswith("claude")
    call = fake.calls[0]
    schema = call["output_config"]["format"]["schema"]
    enum = schema["properties"]["choices"]["items"]["properties"]["candidate_id"]["enum"]
    assert "rate" in enum and "invented" not in enum
    assert call["fallbacks"] == "default"


def test_ai_failure_falls_back_to_rules(demo, cfg, fake_claude):
    fake_claude(RuntimeError("network down"))
    r = run_brief(demo, cfg, force=True, use_ai=True)
    assert r["generator"] == "deterministic rules"
    assert "AI step skipped" in r["markdown"]
    assert one(demo, "SELECT status FROM agent_runs WHERE agent_code = 'DAILY_BI'") == "WARNING"
