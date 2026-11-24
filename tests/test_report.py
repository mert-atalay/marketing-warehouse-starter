"""The static report builds from the marts and shows their totals."""

from runner.report import build


def test_report_shows_mart_totals(con):
    page = build(con)
    spend, leads = con.execute(
        "SELECT SUM(spend), SUM(leads) FROM marts.fct_location_performance").fetchone()
    assert f"${spend:,.0f}" in page
    assert f"{leads:,}" in page
    assert "<script" not in page
    assert "http://" not in page and "https://" not in page
