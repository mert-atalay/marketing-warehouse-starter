import hashlib
from datetime import date

from generator.generate import SCHEMAS, build_tables, generate


def _csv_hashes(directory) -> dict:
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(directory.glob("*.csv"))}


def test_same_seed_gives_identical_files(tmp_path):
    generate(tmp_path / "a", seed=7)
    generate(tmp_path / "b", seed=7)
    hashes = _csv_hashes(tmp_path / "a")
    assert len(hashes) == len(SCHEMAS)
    assert hashes == _csv_hashes(tmp_path / "b")


def test_different_seed_gives_different_data(tmp_path):
    generate(tmp_path / "a", seed=7)
    generate(tmp_path / "b", seed=8)
    assert _csv_hashes(tmp_path / "a")["crm_leads.csv"] != _csv_hashes(tmp_path / "b")["crm_leads.csv"]


def test_csv_and_parquet_are_written_for_every_table(data_dir):
    for name in SCHEMAS:
        assert (data_dir / f"{name}.csv").stat().st_size > 0
        assert (data_dir / f"{name}.parquet").stat().st_size > 0


def test_shape_of_default_data(raw):
    assert len(raw["locations"]) == 12
    assert len(raw["dates"]) == 365  # all of 2025
    # 181 days x 12 locations x 2 campaigns on each ad platform
    assert len(raw["google_ads_campaign_stats"]) == 181 * 12 * 2
    assert len(raw["meta_ads_campaign_stats"]) == 181 * 12 * 2
    lead_ids = [row["lead_id"] for row in raw["crm_leads"]]
    assert len(lead_ids) == len(set(lead_ids)) > 0


def test_date_dimension_week_and_month_starts():
    dates = {row[0]: row for row in build_tables()["dates"]}
    wednesday = dates[date(2025, 3, 12)]
    assert wednesday[2] == date(2025, 3, 10)  # week_start is the Monday
    assert wednesday[3] == date(2025, 3, 1)  # month_start
    assert dates[date(2025, 1, 1)][1] == 1 and dates[date(2025, 12, 31)][1] == 365  # day_seq


def test_stage_history_is_ordered_and_inside_the_date_range(raw):
    stages = {}
    for event in raw["crm_lead_stage_events"]:
        stages.setdefault(event["lead_id"], {})[event["stage"]] = event["event_date"]
        assert event["event_date"] <= "2025-06-30"
    assert len(stages) == len(raw["crm_leads"])  # every lead has at least the lead stage
    for lead in stages.values():
        assert "lead" in lead
        if "member" in lead:
            assert lead["lead"] < lead["trial"] < lead["member"]
            assert "lost" not in lead
