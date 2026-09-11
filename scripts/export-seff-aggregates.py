#!/usr/bin/env python3
"""Export the seff-data gold layer's aggregates into one small committed file.

`analyser.html` reads two payloads. `silver/analyser.json` is folded out of the
RSM snapshot by the hourly workflow and is never committed. This file is the
other one: it comes from the seff-data DuckDB warehouse on m1mz, which the
GitHub Actions runner cannot reach, so it is committed to the repo and refreshed
by running this script locally.

That difference matters to a reader, so the payload carries its own provenance
and the page prints it: anything sourced here is a snapshot taken when someone
last ran this, not the hourly feed.

Three things are exported, none of them read from the `aggregations` schema:

  bandUtilization  How much of each ITU band is actually occupied. The warehouse's
                   own `agg_band_utilization` is wrong — it sums per-assignment
                   bandwidth, so 5 788 VHF clients add up to 985 MHz inside a
                   270 MHz band, and it clamps every row to exactly 100%. Real
                   occupancy is the union of the allocated intervals, computed
                   here from `fact_spectrum_allocation`.

  spectrumUsage    Bandwidth per band x service. Recomputed rather than read from
                   `agg_spectrum_usage`, which last refreshed 2026-01-01.

  expiry           Renewal timeline. Recomputed from `fact_licence.expiry_date`
                   against today, because the warehouse's `days_to_expiry` and
                   `compliance_expiry_tracking` were computed at end-2025 and
                   still call licences that expired in early 2025 "CRITICAL".

    ./scripts/export-seff-aggregates.py
    ./scripts/export-seff-aggregates.py --db ~/seff-data/data/gold.duckdb
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime, timezone

try:
    import duckdb
except ImportError:  # pragma: no cover - environment problem, not logic
    sys.exit("duckdb is not installed. `pip install duckdb`, or run with the "
             "seff-data venv: ~/seff-data/venv/bin/python")

DEFAULT_DB = os.path.expanduser("~/seff-data/data/gold.duckdb")
DEFAULT_OUT = "gold/seff-aggregates.json"

# ITU band edges in Hz. These match sql/enrich.sql and the `bands` block of
# silver/analyser.json — the warehouse's own edges disagree with both (it has HF
# as 0.009-1600 MHz, and UHF and SHF overlapping across 3.3-18 GHz), so they are
# restated here rather than read.
BANDS = [
    ("LF", 30e3, 300e3),
    ("MF", 300e3, 3e6),
    ("HF", 3e6, 30e6),
    ("VHF", 30e6, 300e6),
    ("UHF", 300e6, 3e9),
    ("SHF", 3e9, 30e9),
    ("EHF", 30e9, 300e9),
]

# An allocation wider than this share of its band is a class or band-wide right,
# not an assignment. One of them (a single UHF record spanning 1-18 GHz) is
# enough to peg a whole band at 100% in a union, which would say nothing about
# how congested the band is. They are excluded from the occupancy figure and
# counted separately so the page can say how many were set aside.
BANDWIDE_SHARE = 0.10

BAND_VALUES = ", ".join(f"('{c}', {lo!r}, {hi!r})" for c, lo, hi in BANDS)

UTILISATION_SQL = f"""
with bands(code, lo, hi) as (values {BAND_VALUES}),
raw as (
  select b.code, b.lo as blo, b.hi as bhi,
         greatest(a.frequency_low_hz::double, b.lo) as lo,
         least(coalesce(nullif(a.frequency_high_hz, 0)::double,
                        a.frequency_low_hz::double), b.hi) as hi,
         a.licence_id, a.client_key
  from core_facts.fact_spectrum_allocation a
  join bands b on a.frequency_low_hz::double >= b.lo
              and a.frequency_low_hz::double <  b.hi
  where a.is_current and a.frequency_low_hz is not null
),
tagged as (select *, (hi - lo) > {BANDWIDE_SHARE} * (bhi - blo) as bandwide from raw),
narrow as (select * from tagged where not bandwide),
-- Merge overlapping intervals: an allocation starts a new island when it begins
-- past the furthest point any earlier allocation in the band reached.
ord as (
  select *, max(hi) over (partition by code order by lo
                          rows between unbounded preceding and 1 preceding) as pmax
  from narrow
),
isl as (
  select *, sum(case when pmax is null or lo > pmax then 1 else 0 end)
              over (partition by code order by lo rows unbounded preceding) as grp
  from ord
),
mrg as (select code, blo, bhi, grp, min(lo) as s, max(hi) as e from isl group by 1, 2, 3, 4),
occupied as (
  select code, blo, bhi, sum(e - s) as occ_hz from mrg group by 1, 2, 3
),
counts as (
  select code,
         count(*) as allocations,
         count(distinct client_key) as clients,
         count(*) filter (where bandwide) as bandwide_excluded
  from tagged group by 1
)
select o.code,
       (o.bhi - o.blo) / 1e6            as band_mhz,
       o.occ_hz / 1e6                   as occupied_mhz,
       100.0 * o.occ_hz / (o.bhi - o.blo) as util_pct,
       c.allocations, c.clients, c.bandwide_excluded
from occupied o join counts c using (code)
order by o.blo
"""

USAGE_SQL = """
select band_classification as band,
       service_type        as service,
       count(*)                    as allocations,
       count(distinct client_key)  as clients,
       sum(bandwidth_mhz)::double  as total_bandwidth_mhz,
       avg(bandwidth_mhz)::double  as avg_bandwidth_mhz,
       max(bandwidth_mhz)::double  as max_bandwidth_mhz,
       min(frequency_low_hz)::double  / 1e6 as lowest_mhz,
       max(frequency_high_hz)::double / 1e6 as highest_mhz
from core_facts.fact_spectrum_allocation
where is_current and band_classification is not null
group by 1, 2
having count(*) > 0
order by allocations desc
"""

# Buckets are named for what a holder would do about them, and measured from the
# day the export runs rather than from the warehouse's frozen `days_to_expiry`.
EXPIRY_SQL = """
with cur as (
  select expiry_date, (expiry_date - current_date) as days
  from core_facts.fact_licence
  where is_current and expiry_date is not null
)
select case when days <   0 then 'expired'
            when days <=  30 then 'd30'
            when days <=  90 then 'd90'
            when days <= 365 then 'd365'
            else 'beyond' end as bucket,
       count(*) as n
from cur group by 1
"""

# Transmit AND receive sites. The published register snapshot is transmit-only,
# so the receive side exists nowhere else in this repo — 275 621 records the
# portal has never shown. Coordinates come from the warehouse rather than the
# gazetteer the design prototype carried, which held 70 hand-entered approximate
# points against 23 639 real ones here.
#
# `region`, `district` and `territorial_authority` are present in dim_location
# and empty in every row, so they are not selected — an empty column in the
# payload would read as "this site has no region" rather than "nobody fills
# this in".
#
# Capped, because this file is committed. Publishing all 23 639 sites would add
# ~3.7 MB to git every week, which is the same mistake that forced this
# repository's history rewrite. The busiest sites are the ones a map can
# usefully plot anyway.
#
# COUNT DISTINCT LICENCES, NOT ROWS. `fact_location` is 81.5% redundant — 654 765
# current rows collapse to 121 188 distinct (licence, location, type) triples,
# and one Seddon site carries 21 082 rows for 5 licences. A row count there is a
# measure of the warehouse's duplication, not of activity at the site, so every
# figure below is a distinct-licence count.
SITES_SQL = """
with dedup as (
  select distinct f.licence_id, f.client_key, f.location_key, f.location_type,
         f.effective_radiated_power, f.antenna_height_m
  from core_facts.fact_location f
  where f.is_current
)
select d.location_name                as name,
       round(d.latitude,  5)          as lat,
       round(d.longitude, 5)          as lon,
       round(d.altitude_m, 0)         as alt,
       count(distinct f.licence_id) filter (where f.location_type = 'TRANSMIT') as tx,
       count(distinct f.licence_id) filter (where f.location_type = 'RECEIVE')  as rx,
       count(distinct f.client_key)   as clients,
       count(distinct f.licence_id)   as licences,
       round(max(f.effective_radiated_power), 1) as max_erp,
       round(max(f.antenna_height_m), 1)         as max_ht
from dedup f
join dimensions.dim_location d using (location_key)
where d.latitude  between -48 and -33
  and d.longitude between 165 and 180
group by 1, 2, 3, 4
order by count(distinct f.licence_id) desc
limit {limit}
"""

SITES_TOTALS_SQL = """
with dedup as (
  select distinct f.licence_id, f.location_key, f.location_type
  from core_facts.fact_location f
  where f.is_current
)
select count(*) as site_rows,
       count(*) filter (where f.location_type = 'TRANSMIT') as tx,
       count(*) filter (where f.location_type = 'RECEIVE')  as rx,
       count(distinct d.location_key) as distinct_sites
from dedup f
join dimensions.dim_location d using (location_key)
where d.latitude  between -48 and -33
  and d.longitude between 165 and 180
"""

EXPIRY_MONTHS_SQL = """
select strftime(expiry_date, '%Y-%m') as month, count(*) as n
from core_facts.fact_licence
where is_current and expiry_date is not null
  and expiry_date >= date_trunc('month', current_date)
  and expiry_date <  date_trunc('month', current_date) + interval '24 months'
group by 1 order by 1
"""


def rows(con, sql: str) -> list[dict]:
    cur = con.execute(sql)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def num(v):
    """DuckDB hands back Decimal for the fixed-point columns; JSON will not."""
    if v is None:
        return None
    f = float(v)
    return round(f, 6)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=DEFAULT_DB, help="path to gold.duckdb")
    ap.add_argument("--output", default=DEFAULT_OUT, help="where to write the payload")
    ap.add_argument("--sites", type=int, default=2000,
                    help="how many of the busiest sites to publish (default 2000; "
                         "this file is committed, so the cap keeps git history sane)")
    args = ap.parse_args()

    if not os.path.exists(args.db):
        sys.exit(f"warehouse not found: {args.db}\n"
                 "This export only runs on a machine holding the seff-data warehouse.")

    con = duckdb.connect(args.db, read_only=True)

    util = [{
        "code": r["code"],
        "bandMHz": num(r["band_mhz"]),
        "occupiedMHz": num(r["occupied_mhz"]),
        "utilPct": num(r["util_pct"]),
        "allocations": int(r["allocations"]),
        "clients": int(r["clients"]),
        "bandwideExcluded": int(r["bandwide_excluded"]),
    } for r in rows(con, UTILISATION_SQL)]

    usage = [{
        "band": r["band"],
        "service": r["service"],
        "allocations": int(r["allocations"]),
        "clients": int(r["clients"]),
        "totalBandwidthMHz": num(r["total_bandwidth_mhz"]),
        "avgBandwidthMHz": num(r["avg_bandwidth_mhz"]),
        "maxBandwidthMHz": num(r["max_bandwidth_mhz"]),
        "lowestMHz": num(r["lowest_mhz"]),
        "highestMHz": num(r["highest_mhz"]),
    } for r in rows(con, USAGE_SQL)]

    buckets = {r["bucket"]: int(r["n"]) for r in rows(con, EXPIRY_SQL)}
    months = [{"month": r["month"], "count": int(r["n"])}
              for r in rows(con, EXPIRY_MONTHS_SQL)]

    # Sites go out columnar: nine keys repeated across two thousand objects is
    # most of the file, and none of it is information.
    site_rows = rows(con, SITES_SQL.format(limit=args.sites))
    totals = rows(con, SITES_TOTALS_SQL)[0]
    sites = {
        "n": len(site_rows),
        "distinctSitesAvailable": int(totals["distinct_sites"]),
        "txRecords": int(totals["tx"]),
        "rxRecords": int(totals["rx"]),
        "name": [r["name"] for r in site_rows],
        "lat": [num(r["lat"]) for r in site_rows],
        "lon": [num(r["lon"]) for r in site_rows],
        "alt": [None if r["alt"] is None else int(r["alt"]) for r in site_rows],
        "tx": [int(r["tx"]) for r in site_rows],
        "rx": [int(r["rx"]) for r in site_rows],
        "clients": [int(r["clients"]) for r in site_rows],
        "licences": [int(r["licences"]) for r in site_rows],
        "maxErpW": [num(r["max_erp"]) for r in site_rows],
        "maxHeightM": [num(r["max_ht"]) for r in site_rows],
    }

    # Provenance. The whole point of this file is that it is not the hourly feed,
    # so the page must be able to say how old it is without guessing.
    src = rows(con, """
        select max(updated_at)::date::varchar as licence_fact_updated,
               count(*) as current_licences
        from core_facts.fact_licence where is_current
    """)[0]
    con.close()

    if not util:
        sys.exit("band utilisation came out empty — fact_spectrum_allocation may "
                 "have changed shape. Refusing to write a payload that would "
                 "silently blank the panel.")

    payload = {
        "generatedAt": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        "source": {
            "warehouse": "seff-data gold.duckdb (local to m1mz — not the hourly feed)",
            "warehouseModified": datetime.fromtimestamp(
                os.path.getmtime(args.db), timezone.utc).strftime("%Y-%m-%d"),
            "licenceFactUpdated": src["licence_fact_updated"],
            "currentLicences": int(src["current_licences"]),
            "expiryComputedOn": date.today().isoformat(),
            "bandwideShareExcluded": BANDWIDE_SHARE,
        },
        "sites": sites,
        "bandUtilization": util,
        "spectrumUsage": usage,
        "expiry": {"buckets": buckets, "months": months},
    }

    os.makedirs(os.path.dirname(args.output) or ".", exist_ok=True)
    with open(args.output, "w") as fh:
        json.dump(payload, fh, separators=(",", ":"), sort_keys=False)
        fh.write("\n")

    size = os.path.getsize(args.output)
    print(f"wrote {args.output} ({size / 1024:.1f} KB) — "
          f"{len(util)} bands, {len(usage)} band x service rows, "
          f"{len(months)} expiry months, {sites['n']} sites "
          f"of {sites['distinctSitesAvailable']:,}")
    print(f"  warehouse last modified {payload['source']['warehouseModified']}, "
          f"licence facts {src['licence_fact_updated']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
