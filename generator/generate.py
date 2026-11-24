"""Generate synthetic marketing data for the fictional Harbour Fitness.

Everything is driven by one seeded random.Random, so the same seed and date
range always produce the same files. CSV is written first with the standard
library. Parquet is then derived from the CSV with explicit column types.

Raw tables written to the output directory:

    locations                  one row per gym location
    dates                      one row per calendar day (full calendar years)
    google_ads_campaign_stats  one row per day and campaign
    meta_ads_campaign_stats    one row per day and campaign
    ga4_sessions               one row per day, location, source, medium, campaign
    crm_leads                  one row per lead
    crm_lead_stage_events      one row per lead and stage reached
"""

import argparse
import csv
import math
import random
from datetime import date, timedelta
from pathlib import Path

import duckdb

DEFAULT_SEED = 42
DEFAULT_START = date(2025, 1, 1)
DEFAULT_END = date(2025, 6, 30)

# (location_id, name, region, opened_date, size factor). The size factor scales
# traffic and spend. It is not written to the output.
LOCATIONS = [
    (1, "Harbour Fitness Downtown", "Central", "2016-03-01", 1.6),
    (2, "Harbour Fitness Midtown", "Central", "2017-09-15", 1.4),
    (3, "Harbour Fitness Riverside", "Central", "2019-01-10", 1.2),
    (4, "Harbour Fitness Northgate", "North", "2018-05-20", 1.1),
    (5, "Harbour Fitness Hillcrest", "North", "2021-02-01", 0.9),
    (6, "Harbour Fitness Lakeview", "North", "2022-08-12", 0.7),
    (7, "Harbour Fitness Eastside", "East", "2019-11-04", 1.0),
    (8, "Harbour Fitness Oakfield", "East", "2023-04-17", 0.6),
    (9, "Harbour Fitness Southport", "South", "2018-10-08", 1.2),
    (10, "Harbour Fitness Brookside", "South", "2020-06-22", 0.8),
    (11, "Harbour Fitness Westbrook", "West", "2020-01-13", 1.0),
    (12, "Harbour Fitness Seaview", "West", "2024-03-05", 0.7),
]

# Paid campaigns. Each location runs its own copy of every campaign.
PAID_CAMPAIGNS = [
    # platform, source, medium, campaign, clicks/day, cpc, ctr, form rate
    ("google", "google", "cpc", "brand_search", 16, 1.40, 0.120, 0.080),
    ("google", "google", "cpc", "nonbrand_search", 18, 3.20, 0.040, 0.040),
    ("meta", "facebook", "paid_social", "trial_offer", 36, 1.25, 0.012, 0.030),
    ("meta", "facebook", "paid_social", "retargeting", 14, 0.90, 0.020, 0.050),
]

# Unpaid channels: source, medium, campaign, sessions/day, form rate.
UNPAID_CHANNELS = [
    ("google", "organic", "(not set)", 60, 0.030),
    ("(direct)", "(none)", "(not set)", 40, 0.025),
    ("newsletter", "email", "monthly_newsletter", 8, 0.040),
    ("partner", "referral", "(not set)", 6, 0.050),
]

CLICK_TO_SESSION_RATE = 0.90  # some ad clicks never become a session
FORM_TO_LEAD_RATE = 0.85  # some form submits are duplicates or spam

# Share of leads that book a trial, and share of trials that become members.
TRIAL_RATE = {"cpc": 0.45, "paid_social": 0.30, "organic": 0.45, "(none)": 0.40, "email": 0.50, "referral": 0.55}
MEMBER_RATE = {"cpc": 0.55, "paid_social": 0.45, "organic": 0.55, "(none)": 0.55, "email": 0.60, "referral": 0.60}

WEEKDAY_FACTOR = [1.10, 1.05, 1.00, 0.95, 0.85, 0.75, 0.90]  # Monday first

# Column types used when converting each CSV to Parquet.
SCHEMAS = {
    "locations": {"location_id": "INTEGER", "location_name": "VARCHAR", "region": "VARCHAR", "opened_date": "DATE"},
    "dates": {
        "date_day": "DATE",
        "day_seq": "INTEGER",
        "week_start": "DATE",
        "month_start": "DATE",
        "year_number": "INTEGER",
        "month_number": "INTEGER",
        "day_of_week": "INTEGER",
        "is_weekend": "BOOLEAN",
    },
    "google_ads_campaign_stats": {
        "segments_date": "DATE",
        "campaign_id": "BIGINT",
        "campaign_name": "VARCHAR",
        "location_id": "INTEGER",
        "impressions": "BIGINT",
        "clicks": "BIGINT",
        "cost_micros": "BIGINT",
    },
    "meta_ads_campaign_stats": {
        "date_start": "DATE",
        "campaign_id": "BIGINT",
        "campaign_name": "VARCHAR",
        "location_id": "INTEGER",
        "impressions": "BIGINT",
        "clicks": "BIGINT",
        "spend": "DOUBLE",
    },
    "ga4_sessions": {
        "event_date": "DATE",
        "location_id": "INTEGER",
        "source": "VARCHAR",
        "medium": "VARCHAR",
        "campaign": "VARCHAR",
        "sessions": "BIGINT",
        "form_submits": "BIGINT",
    },
    "crm_leads": {
        "lead_id": "VARCHAR",
        "created_date": "DATE",
        "location_id": "INTEGER",
        "utm_source": "VARCHAR",
        "utm_medium": "VARCHAR",
        "utm_campaign": "VARCHAR",
    },
    "crm_lead_stage_events": {"lead_id": "VARCHAR", "stage": "VARCHAR", "event_date": "DATE"},
}


def _noisy_count(rng: random.Random, mean: float) -> int:
    """A non-negative integer around `mean`, roughly Poisson shaped."""
    return max(0, round(rng.gauss(mean, math.sqrt(mean))))


def _day_factor(day: date) -> float:
    """Weekday pattern plus a January bump, which gyms tend to see."""
    return WEEKDAY_FACTOR[day.weekday()] * (1.35 if day.month == 1 else 1.0)


def _crm_utm(rng: random.Random, source: str, medium: str, campaign: str) -> tuple:
    """UTM values as a CRM would store them: blanks and inconsistent casing.

    The staging model is expected to clean this up again.
    """
    if medium == "(none)":
        return (None, None, None)
    messy = rng.random()
    if messy < 0.03:
        source = source.title()
    elif messy < 0.05:
        source = source + " "
    return (source, medium, None if campaign == "(not set)" else campaign)


def build_tables(seed: int = DEFAULT_SEED, start: date = DEFAULT_START, end: date = DEFAULT_END) -> dict:
    """Return {table name: list of row tuples} in SCHEMAS column order."""
    if end < start:
        raise ValueError("end date is before start date")
    rng = random.Random(seed)

    locations = [row[:4] for row in LOCATIONS]

    dates = []
    day = date(start.year, 1, 1)
    seq = 1
    while day <= date(end.year, 12, 31):
        dates.append(
            (
                day,
                seq,
                day - timedelta(days=day.weekday()),  # Monday of that week
                day.replace(day=1),
                day.year,
                day.month,
                day.weekday() + 1,  # 1 = Monday
                day.weekday() >= 5,
            )
        )
        day += timedelta(days=1)
        seq += 1

    google_ads, meta_ads, sessions, leads, events = [], [], [], [], []

    def add_leads(day: date, location_id: int, source: str, medium: str, campaign: str, count: int) -> None:
        for _ in range(count):
            lead_id = f"L{len(leads) + 1:06d}"
            leads.append((lead_id, day, location_id, *_crm_utm(rng, source, medium, campaign)))
            stage_dates = [("lead", day)]
            if rng.random() < TRIAL_RATE[medium]:
                trial_date = day + timedelta(days=rng.randint(1, 10))
                stage_dates.append(("trial", trial_date))
                if rng.random() < MEMBER_RATE[medium]:
                    stage_dates.append(("member", trial_date + timedelta(days=rng.randint(3, 14))))
                elif rng.random() < 0.70:
                    stage_dates.append(("lost", trial_date + timedelta(days=rng.randint(7, 21))))
            elif rng.random() < 0.60:
                stage_dates.append(("lost", day + timedelta(days=rng.randint(14, 30))))
            # Stages dated after the end of the data have not happened yet.
            events.extend((lead_id, stage, d) for stage, d in stage_dates if d <= end)

    day = start
    while day <= end:
        factor = _day_factor(day)
        for location_id, _, _, _, size in LOCATIONS:
            for index, (platform, source, medium, campaign, clicks_mean, cpc, ctr, form_rate) in enumerate(
                PAID_CAMPAIGNS
            ):
                clicks = _noisy_count(rng, clicks_mean * size * factor)
                spend = round(clicks * cpc * rng.uniform(0.85, 1.15), 2)
                impressions = round(clicks / ctr * rng.uniform(0.9, 1.1))
                if platform == "google":
                    campaign_id = 1000 + location_id * 10 + index
                    cost_micros = round(spend * 100) * 10_000
                    google_ads.append((day, campaign_id, campaign, location_id, impressions, clicks, cost_micros))
                else:
                    campaign_id = 2000 + location_id * 10 + index
                    meta_ads.append((day, campaign_id, campaign, location_id, impressions, clicks, spend))

                session_count = rng.binomialvariate(clicks, CLICK_TO_SESSION_RATE) if clicks else 0
                if session_count == 0:
                    continue  # spend with no recorded session does happen
                form_submits = rng.binomialvariate(session_count, form_rate)
                sessions.append((day, location_id, source, medium, campaign, session_count, form_submits))
                lead_count = rng.binomialvariate(form_submits, FORM_TO_LEAD_RATE) if form_submits else 0
                add_leads(day, location_id, source, medium, campaign, lead_count)

            for source, medium, campaign, sessions_mean, form_rate in UNPAID_CHANNELS:
                session_count = _noisy_count(rng, sessions_mean * size * factor)
                if session_count == 0:
                    continue
                form_submits = rng.binomialvariate(session_count, form_rate)
                sessions.append((day, location_id, source, medium, campaign, session_count, form_submits))
                lead_count = rng.binomialvariate(form_submits, FORM_TO_LEAD_RATE) if form_submits else 0
                add_leads(day, location_id, source, medium, campaign, lead_count)
        day += timedelta(days=1)

    return {
        "locations": locations,
        "dates": dates,
        "google_ads_campaign_stats": google_ads,
        "meta_ads_campaign_stats": meta_ads,
        "ga4_sessions": sessions,
        "crm_leads": leads,
        "crm_lead_stage_events": events,
    }


def _cell(value):
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return value


def write_tables(tables: dict, out_dir: Path) -> None:
    """Write every table as <name>.csv and <name>.parquet in out_dir."""
    out_dir.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    for name, rows in tables.items():
        columns = SCHEMAS[name]
        csv_path = out_dir / f"{name}.csv"
        with csv_path.open("w", newline="") as handle:
            writer = csv.writer(handle, lineterminator="\n")
            writer.writerow(columns)
            writer.writerows([_cell(v) for v in row] for row in rows)
        parquet_path = out_dir / f"{name}.parquet"
        con.read_csv(str(csv_path), header=True, columns=columns).write_parquet(str(parquet_path))
    con.close()


def generate(out_dir: Path, seed: int = DEFAULT_SEED, start: date = DEFAULT_START, end: date = DEFAULT_END) -> dict:
    """Build and write all tables. Returns {table name: row count}."""
    tables = build_tables(seed, start, end)
    write_tables(tables, Path(out_dir))
    return {name: len(rows) for name, rows in tables.items()}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", default="data", help="output directory (default: data)")
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--start", type=date.fromisoformat, default=DEFAULT_START)
    parser.add_argument("--end", type=date.fromisoformat, default=DEFAULT_END)
    args = parser.parse_args()

    counts = generate(Path(args.out), args.seed, args.start, args.end)
    print(f"Wrote synthetic data to {args.out}/ (seed {args.seed}, {args.start} to {args.end})")
    for name, count in counts.items():
        print(f"  {name:<28}{count:>8} rows")
