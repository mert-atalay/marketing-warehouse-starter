"""Write a small static HTML report from the built marts.

Usage: python -m runner.report [--db warehouse.duckdb] [--out report.html]

The page is self-contained: inline CSS and inline SVG, no scripts, no
external requests. Every number on it is read from the mart tables.
"""

from __future__ import annotations

import argparse
import html
from pathlib import Path

import duckdb

CSS = """
:root { --bg:#f6f7f9; --card:#fff; --ink:#16202c; --muted:#5d6b7a; --line:#e3e7ec;
        --a:#2563a8; --b:#e08a2b; --c:#2f8f6b; }
@media (prefers-color-scheme: dark) {
  :root { --bg:#12161c; --card:#1b212a; --ink:#e8edf2; --muted:#9aa7b5; --line:#2b3440;
          --a:#6aa6e8; --b:#f0a850; --c:#5fc49c; }
}
* { box-sizing: border-box; }
body { margin:0; background:var(--bg); color:var(--ink);
       font:15px/1.45 -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; }
main { max-width:1080px; margin:0 auto; padding:28px 16px 40px; }
h1 { font-size:22px; margin:0 0 4px; }
h2 { font-size:15px; margin:0 0 12px; }
.sub { color:var(--muted); margin:0 0 20px; }
.grid { display:grid; gap:14px; }
.kpis { grid-template-columns:repeat(auto-fit, minmax(150px, 1fr)); margin-bottom:14px; }
.two { grid-template-columns:repeat(auto-fit, minmax(320px, 1fr)); margin-bottom:14px; }
.card { background:var(--card); border:1px solid var(--line); border-radius:10px; padding:16px; }
.kpi .label { color:var(--muted); font-size:13px; }
.kpi .value { font-size:26px; font-weight:650; font-variant-numeric:tabular-nums; }
table { width:100%; border-collapse:collapse; font-variant-numeric:tabular-nums; }
th, td { padding:7px 6px; border-bottom:1px solid var(--line); text-align:right; white-space:nowrap; }
th:first-child, td:first-child { text-align:left; }
th { color:var(--muted); font-weight:550; font-size:13px; }
tr:last-child td { border-bottom:0; }
.bar { display:inline-block; height:9px; border-radius:3px; background:var(--a); vertical-align:middle; margin-right:8px; }
.legend { color:var(--muted); font-size:13px; margin-top:8px; }
.dot { display:inline-block; width:10px; height:10px; border-radius:2px; margin:0 5px 0 12px; }
.scroll { overflow-x:auto; }
svg text { fill:var(--muted); font-size:12px; }
footer { color:var(--muted); font-size:13px; margin-top:18px; }
"""


def money(value: float) -> str:
    return f"${value:,.0f}"


def monthly_chart(rows: list[tuple]) -> str:
    """Grouped bars: spend (left scale) and leads (right scale) per month."""
    width, height, pad_l, pad_b, pad_t = 460, 220, 8, 26, 10
    plot_h = height - pad_b - pad_t
    max_spend = max(r[1] for r in rows) or 1
    max_leads = max(r[2] for r in rows) or 1
    slot = (width - pad_l * 2) / len(rows)
    bar_w = slot * 0.3
    parts = [f'<svg viewBox="0 0 {width} {height}" role="img" '
             f'aria-label="Monthly spend and leads" width="100%">']
    for i, (month, spend, leads) in enumerate(rows):
        x = pad_l + i * slot + slot * 0.17
        h_spend = plot_h * spend / max_spend
        h_leads = plot_h * leads / max_leads
        base = pad_t + plot_h
        parts.append(f'<rect x="{x:.1f}" y="{base - h_spend:.1f}" width="{bar_w:.1f}" '
                     f'height="{h_spend:.1f}" rx="2" fill="var(--a)"><title>{month}: '
                     f'{money(spend)} spend</title></rect>')
        parts.append(f'<rect x="{x + bar_w + 3:.1f}" y="{base - h_leads:.1f}" width="{bar_w:.1f}" '
                     f'height="{h_leads:.1f}" rx="2" fill="var(--b)"><title>{month}: '
                     f'{leads:,} leads</title></rect>')
        parts.append(f'<text x="{x + bar_w:.1f}" y="{height - 8}" text-anchor="middle">'
                     f'{html.escape(month)}</text>')
    parts.append("</svg>")
    return "".join(parts)


def build(con: duckdb.DuckDBPyConnection) -> str:
    """Return the report page as a string. Reads only from the marts."""
    spend, leads, trials, members = con.execute(
        "SELECT SUM(spend), SUM(leads), SUM(trials), SUM(members) "
        "FROM marts.fct_location_performance").fetchone()
    first, last = con.execute(
        "SELECT MIN(month_start), MAX(month_start) FROM marts.fct_location_performance").fetchone()
    monthly = con.execute(
        "SELECT strftime(month_start, '%b'), SUM(spend), SUM(leads) "
        "FROM marts.fct_location_performance GROUP BY month_start ORDER BY month_start").fetchall()
    locations = con.execute(
        "SELECT location_name, SUM(spend), SUM(leads), SUM(spend) / NULLIF(SUM(leads), 0), "
        "SUM(members), SUM(spend) / NULLIF(SUM(members), 0) "
        "FROM marts.fct_location_performance GROUP BY location_name ORDER BY 6").fetchall()
    funnel = con.execute(
        "SELECT channel_group, SUM(leads), SUM(trials), SUM(members) "
        "FROM marts.fct_lead_funnel GROUP BY channel_group ORDER BY 2 DESC").fetchall()

    kpis = [
        ("Ad spend", money(spend)),
        ("Leads", f"{leads:,}"),
        ("Cost per lead", f"${spend / leads:,.2f}"),
        ("Trials", f"{trials:,}"),
        ("Members", f"{members:,}"),
        ("Cost per member", f"${spend / members:,.2f}"),
    ]
    kpi_html = "".join(
        f'<div class="card kpi"><div class="label">{label}</div>'
        f'<div class="value">{value}</div></div>' for label, value in kpis)

    max_cpm = max(r[5] for r in locations)
    loc_rows = "".join(
        f"<tr><td>{html.escape(name)}</td><td>{money(sp)}</td><td>{ld:,}</td>"
        f"<td>${cpl:,.2f}</td><td>{mem:,}</td>"
        f'<td><span class="bar" style="width:{70 * cpm / max_cpm:.0f}px"></span>${cpm:,.2f}</td></tr>'
        for name, sp, ld, cpl, mem, cpm in locations)

    funnel_rows = "".join(
        f"<tr><td>{html.escape(str(group))}</td><td>{ld:,}</td><td>{tr:,}</td><td>{mem:,}</td>"
        f"<td>{tr / ld:.0%}</td><td>{mem / ld:.0%}</td></tr>"
        for group, ld, tr, mem in funnel)

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Harbour Fitness marketing report (synthetic data)</title>
<style>{CSS}</style></head><body><main>
<h1>Harbour Fitness: marketing performance</h1>
<p class="sub">Synthetic data for a fictional business, {first:%B %Y} to {last:%B %Y}.
Built from the mart tables in this repository.</p>
<div class="grid kpis">{kpi_html}</div>
<div class="grid two">
  <div class="card"><h2>Spend and leads by month</h2>{monthly_chart(monthly)}
    <div class="legend"><span class="dot" style="background:var(--a);margin-left:0"></span>Ad spend
    <span class="dot" style="background:var(--b)"></span>Leads (separate scales)</div></div>
  <div class="card"><h2>Lead funnel by channel group</h2><div class="scroll"><table>
    <tr><th>Channel</th><th>Leads</th><th>Trials</th><th>Members</th><th>To trial</th><th>To member</th></tr>
    {funnel_rows}</table></div></div>
</div>
<div class="card"><h2>Locations, lowest cost per member first</h2><div class="scroll"><table>
  <tr><th>Location</th><th>Spend</th><th>Leads</th><th>Cost per lead</th><th>Members</th><th>Cost per member</th></tr>
  {loc_rows}</table></div></div>
<footer>Cost per lead and cost per member divide all ad spend by all leads or members,
including unpaid ones. Members are counted in the month their lead was created.</footer>
</main></body></html>
"""


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="warehouse.duckdb")
    parser.add_argument("--out", default="report.html")
    args = parser.parse_args()
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(args.db, read_only=True)
    out.write_text(build(con), encoding="utf-8")
    con.close()
    print(f"Wrote {out}")


if __name__ == "__main__":
    main()
