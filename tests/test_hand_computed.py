"""A tiny hand-written dataset where every expected number can be checked on paper.

One location, three days in March 2025.

    Ad spend   Google brand_search  40.00 + 60.00   = 100.00
               Meta trial_offer     50.00           =  50.00
               total                                  150.00
    Leads      A google/cpc     -> trial -> member
               B google/cpc     -> trial
               C facebook       -> lost
               D facebook       (still open)
               E no UTM values  -> trial -> member   (direct)
               F google/organic (still open)

    cost per lead       150 / 6 = 25.00
    cost per paid lead  150 / 4 = 37.50
    cost per trial      150 / 3 = 50.00
    cost per member     150 / 2 = 75.00
"""

from datetime import date

import duckdb
import pytest

from runner.run import build_models
from runner.sqlx import load_project
from tests.conftest import PROJECT_DIR

RAW_TABLES = {
    "locations": "SELECT 1 AS location_id, 'Harbour Fitness Test' AS location_name, 'Central' AS region, "
    "DATE '2020-01-01' AS opened_date",
    "dates": "SELECT CAST(d AS DATE) AS date_day, CAST(row_number() OVER (ORDER BY d) AS INTEGER) AS day_seq, "
    "CAST(date_trunc('week', d) AS DATE) AS week_start, CAST(date_trunc('month', d) AS DATE) AS month_start, "
    "2025 AS year_number, 3 AS month_number, isodow(d) AS day_of_week, isodow(d) >= 6 AS is_weekend "
    "FROM generate_series(DATE '2025-03-01', DATE '2025-03-31', INTERVAL 1 DAY) AS t(d)",
    "google_ads_campaign_stats": """
        SELECT * FROM (VALUES
            (DATE '2025-03-03', 1010, 'brand_search', 1, 500, 20, 40000000),
            (DATE '2025-03-04', 1010, 'brand_search', 1, 700, 30, 60000000)
        ) AS t(segments_date, campaign_id, campaign_name, location_id, impressions, clicks, cost_micros)""",
    "meta_ads_campaign_stats": """
        SELECT * FROM (VALUES
            (DATE '2025-03-03', 2012, 'trial_offer', 1, 4000, 40, 50.0)
        ) AS t(date_start, campaign_id, campaign_name, location_id, impressions, clicks, spend)""",
    "ga4_sessions": """
        SELECT * FROM (VALUES
            (DATE '2025-03-03', 1, 'google', 'cpc', 'brand_search', 18, 2),
            (DATE '2025-03-03', 1, 'facebook', 'paid_social', 'trial_offer', 35, 2),
            (DATE '2025-03-04', 1, '(direct)', '(none)', '(not set)', 10, 1),
            (DATE '2025-03-05', 1, 'google', 'organic', '(not set)', 12, 1)
        ) AS t(event_date, location_id, source, medium, campaign, sessions, form_submits)""",
    "crm_leads": """
        SELECT * FROM (VALUES
            ('A', DATE '2025-03-03', 1, 'google', 'cpc', 'brand_search'),
            ('B', DATE '2025-03-03', 1, 'Google ', 'cpc', 'brand_search'),
            ('C', DATE '2025-03-03', 1, 'facebook', 'paid_social', 'trial_offer'),
            ('D', DATE '2025-03-03', 1, 'Facebook', 'paid_social', 'trial_offer'),
            ('E', DATE '2025-03-04', 1, NULL, NULL, NULL),
            ('F', DATE '2025-03-05', 1, 'google', 'organic', NULL)
        ) AS t(lead_id, created_date, location_id, utm_source, utm_medium, utm_campaign)""",
    "crm_lead_stage_events": """
        SELECT * FROM (VALUES
            ('A', 'lead', DATE '2025-03-03'), ('A', 'trial', DATE '2025-03-05'), ('A', 'member', DATE '2025-03-12'),
            ('B', 'lead', DATE '2025-03-03'), ('B', 'trial', DATE '2025-03-07'),
            ('C', 'lead', DATE '2025-03-03'), ('C', 'lost', DATE '2025-03-20'),
            ('D', 'lead', DATE '2025-03-03'),
            ('E', 'lead', DATE '2025-03-04'), ('E', 'trial', DATE '2025-03-06'), ('E', 'member', DATE '2025-03-10'),
            ('F', 'lead', DATE '2025-03-05')
        ) AS t(lead_id, stage, event_date)""",
}


@pytest.fixture(scope="module")
def tiny():
    con = duckdb.connect()
    con.execute("CREATE SCHEMA raw")
    for name, query in RAW_TABLES.items():
        con.execute(f"CREATE TABLE raw.{name} AS {query}")
    build_models(con, load_project(PROJECT_DIR))
    yield con
    con.close()


def test_location_performance(tiny):
    rows = tiny.execute(
        "SELECT month_start, location_id, sessions, form_submits, spend, leads, paid_leads, trials, members, "
        "cost_per_lead, cost_per_paid_lead, cost_per_trial, cost_per_member FROM marts.fct_location_performance"
    ).fetchall()
    assert rows == [(date(2025, 3, 1), 1, 75, 6, 150.0, 6, 4, 3, 2, 25.0, 37.5, 50.0, 75.0)]


def test_daily_channel_performance(tiny):
    rows = tiny.execute(
        "SELECT date_day, week_start, channel_group, campaign, sessions, clicks, spend, leads, cost_per_lead "
        "FROM marts.fct_daily_channel_performance ORDER BY date_day, channel_group"
    ).fetchall()
    monday = date(2025, 3, 3)
    assert rows == [
        (date(2025, 3, 3), monday, "paid_search", "brand_search", 18, 20, 40.0, 2, 20.0),
        (date(2025, 3, 3), monday, "paid_social", "trial_offer", 35, 40, 50.0, 2, 25.0),
        (date(2025, 3, 4), monday, "direct", "(not set)", 10, 0, 0.0, 1, 0.0),
        # Spend with no session and no lead that day. cost_per_lead is NULL, not an error.
        (date(2025, 3, 4), monday, "paid_search", "brand_search", 0, 30, 60.0, 0, None),
        (date(2025, 3, 5), monday, "organic_search", "(not set)", 12, 0, 0.0, 1, 0.0),
    ]


def test_lead_funnel(tiny):
    rows = tiny.execute(
        "SELECT channel_group, leads, trials, members, lost, open_leads, lead_to_trial_rate, "
        "trial_to_member_rate, avg_days_to_trial, avg_days_to_member "
        "FROM marts.fct_lead_funnel ORDER BY channel_group"
    ).fetchall()
    assert rows == [
        ("direct", 1, 1, 1, 0, 0, 1.0, 1.0, 2.0, 6.0),
        ("organic_search", 1, 0, 0, 0, 1, 0.0, None, None, None),
        ("paid_search", 2, 2, 1, 0, 1, 1.0, 0.5, 3.0, 9.0),  # trials after 2 and 4 days
        ("paid_social", 2, 0, 0, 1, 1, 0.0, None, None, None),
    ]
