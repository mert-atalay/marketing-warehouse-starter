"""Build the real models on the default synthetic data and check known answers.

Expected values are computed from the raw CSV rows in plain Python.
"""

import duckdb
import pytest

from runner.run import run, run_assertions
from runner.sqlx import load_project
from tests.conftest import PROJECT_DIR

EXPECTED_MODELS = {
    "staging": {
        "stg_crm_lead_stage_events",
        "stg_crm_leads",
        "stg_dates",
        "stg_ga4_sessions",
        "stg_google_ads",
        "stg_locations",
        "stg_meta_ads",
    },
    "intermediate": {"int_ad_spend_daily", "int_channel_groups", "int_leads_daily", "int_leads_enriched"},
    "marts": {"fct_daily_channel_performance", "fct_lead_funnel", "fct_location_performance"},
}


def _raw_spend(raw, keep=lambda row, day: True) -> float:
    google = sum(int(r["cost_micros"]) for r in raw["google_ads_campaign_stats"] if keep(r, r["segments_date"]))
    meta = sum(float(r["spend"]) for r in raw["meta_ads_campaign_stats"] if keep(r, r["date_start"]))
    return google / 1_000_000 + meta


def test_all_models_build_with_rows(con):
    rows = con.execute(
        "SELECT table_schema, table_name FROM information_schema.tables WHERE table_schema <> 'raw'"
    ).fetchall()
    built = {}
    for schema, name in rows:
        built.setdefault(schema, set()).add(name)
    assert built == EXPECTED_MODELS
    for schema, names in EXPECTED_MODELS.items():
        for name in names:
            assert con.execute(f"SELECT COUNT(*) FROM {schema}.{name}").fetchone()[0] > 0


def test_all_assertions_pass(built):
    results = built[1]
    assert {r.name for r in results} == {
        "assert_unique_keys",
        "assert_non_null_keys",
        "assert_spend_reconciles",
        "assert_lead_counts_reconcile",
        "assert_funnel_is_monotonic",
        "assert_marts_are_fresh",
    }
    assert [r.name for r in results if not r.passed] == []


def test_mart_spend_equals_generated_ad_spend(con, raw):
    expected = _raw_spend(raw)
    assert expected > 0
    for mart in ("fct_daily_channel_performance", "fct_location_performance"):
        actual = con.execute(f"SELECT SUM(spend) FROM marts.{mart}").fetchone()[0]
        assert actual == pytest.approx(expected, abs=0.01)


def test_mart_sessions_and_leads_equal_generated_totals(con, raw):
    sessions, leads = con.execute(
        "SELECT SUM(sessions), SUM(leads) FROM marts.fct_daily_channel_performance"
    ).fetchone()
    assert sessions == sum(int(r["sessions"]) for r in raw["ga4_sessions"])
    assert leads == len(raw["crm_leads"])
    assert con.execute("SELECT SUM(leads) FROM marts.fct_lead_funnel").fetchone()[0] == leads


def test_cost_per_lead_for_one_location_and_month(con, raw):
    """Location 3, March 2025: all ad spend divided by all leads created."""
    in_scope = lambda row, day: row["location_id"] == "3" and day.startswith("2025-03")
    spend = _raw_spend(raw, in_scope)
    leads = sum(1 for r in raw["crm_leads"] if in_scope(r, r["created_date"]))
    assert spend > 0 and leads > 0

    row = con.execute(
        "SELECT spend, leads, cost_per_lead FROM marts.fct_location_performance "
        "WHERE location_id = 3 AND month_start = DATE '2025-03-01'"
    ).fetchone()
    assert row[0] == pytest.approx(spend, abs=0.01)
    assert row[1] == leads
    assert row[2] == pytest.approx(spend / leads, rel=1e-9)


def test_funnel_counts_match_stage_history(con, raw):
    reached = {"trial": set(), "member": set(), "lost": set()}
    for event in raw["crm_lead_stage_events"]:
        if event["stage"] in reached:
            reached[event["stage"]].add(event["lead_id"])
    leads, trials, members, lost = con.execute(
        "SELECT SUM(leads), SUM(trials), SUM(members), SUM(lost) FROM marts.fct_lead_funnel"
    ).fetchone()
    assert (trials, members, lost) == (len(reached["trial"]), len(reached["member"]), len(reached["lost"]))
    assert leads >= trials >= members > 0


def test_funnel_is_monotonic_in_every_row(con):
    broken = con.execute(
        "SELECT COUNT(*) FROM marts.fct_lead_funnel WHERE NOT (leads >= trials AND trials >= members)"
    ).fetchone()[0]
    assert broken == 0


def test_messy_crm_utm_values_are_cleaned(con, raw):
    messy = [r for r in raw["crm_leads"] if r["utm_source"] not in ("", r["utm_source"].strip().lower())]
    assert messy, "the generator should produce some badly formatted utm_source values"
    sources = {row[0] for row in con.execute("SELECT DISTINCT source FROM staging.stg_crm_leads").fetchall()}
    assert sources == {"google", "facebook", "newsletter", "partner", "(direct)"}
    # Leads with no UTM values at all are counted as direct, not dropped.
    direct = con.execute("SELECT SUM(leads) FROM marts.fct_lead_funnel WHERE channel_group = 'direct'").fetchone()[0]
    assert direct == sum(1 for r in raw["crm_leads"] if r["utm_medium"] == "")


def test_only_paid_channels_carry_spend(con):
    groups = con.execute(
        "SELECT DISTINCT channel_group FROM marts.fct_daily_channel_performance WHERE spend > 0"
    ).fetchall()
    assert {g[0] for g in groups} == {"paid_search", "paid_social"}


# The assertions must also fail when the data is wrong. Each case breaks a fresh
# build of the warehouse in one way and names the assertion that should catch it.
BREAKAGES = {
    "assert_spend_reconciles": "UPDATE marts.fct_daily_channel_performance SET spend = spend + 5 "
    "WHERE date_day = DATE '2025-02-10' AND location_id = 1 AND campaign = 'brand_search'",
    "assert_unique_keys": "INSERT INTO marts.fct_location_performance "
    "SELECT * FROM marts.fct_location_performance WHERE location_id = 2 AND month_start = DATE '2025-01-01'",
    "assert_non_null_keys": "UPDATE marts.fct_lead_funnel SET channel_group = NULL WHERE location_id = 4",
    "assert_lead_counts_reconcile": "DELETE FROM marts.fct_lead_funnel WHERE location_id = 5",
    "assert_funnel_is_monotonic": "UPDATE marts.fct_lead_funnel SET members = trials + 1 WHERE location_id = 6",
    "assert_marts_are_fresh": "DELETE FROM marts.fct_daily_channel_performance WHERE date_day > DATE '2025-06-20'",
}


@pytest.mark.parametrize("assertion_name", sorted(BREAKAGES))
def test_assertion_fails_on_broken_data(data_dir, assertion_name):
    broken = duckdb.connect()
    assert all(r.passed for r in run(broken, data_dir, PROJECT_DIR))
    broken.execute(BREAKAGES[assertion_name])

    failed = {r.name for r in run_assertions(broken, load_project(PROJECT_DIR)) if not r.passed}
    broken.close()
    assert assertion_name in failed
