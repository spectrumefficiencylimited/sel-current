#!/usr/bin/env python3
"""Export the browser query indexes for the analyser, once a day, from seff-data gold.

The analyser is a cross-filtering tool: clicking a band, a matrix cell, a
licence row or a holder has to re-derive every other panel. That is only
possible if the browser holds the individual records, not pre-aggregated
summaries — an aggregate cannot be filtered back down.

Two indexes, because the register has two grains and forcing them into one
would multiply out:

  spectrum  one row per (licence x spectrum allocation) - 74 k rows.
            Drives tuning, band allocation, the occupancy matrix, the result
            set and the holdings panel.
  sites     one row per (licence x location x direction) - 121 k rows.
            Drives the map, including the TX/RX toggle, which the summary API
            cannot support because it carries no receive side at all.

Both are joined in the browser on licence_id, so a selection made in one panel
reaches the other.

Shape: columnar rather than a list of objects (no key name repeated 74 000
times), dictionary-encoded for the text columns, frequencies as integers in
kHz. Roughly 6 MB of JSON that leaves a server as ~900 KB gzipped.

This reads seff-data's gold layer READ-ONLY and writes only into this
repository. The two pipelines stay separate: seff-data owns the warehouse,
sel-current owns the site, and this is the one-way hand-off between them.

    ./scripts/export-prism-index.py
    ./scripts/export-prism-index.py --gold ~/seff-data/data/gold.duckdb --out silver
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import duckdb

GOLD = Path.home() / "seff-data" / "data" / "gold.duckdb"

# One row per licence x spectrum allocation. Frequencies in kHz as integers:
# the register's finest step is 1 kHz and "162.02500" costs four times what
# 162025 does.
SPECTRUM_SQL = """
SELECT
    s.licence_id                                   AS id,
    COALESCE(CAST(f.licence_number AS VARCHAR), '') AS num,
    COALESCE(c.client_name, 'UNKNOWN')             AS lic,
    COALESCE(f.licence_type, '')                   AS lt,
    COALESCE(f.licence_status, 'UNKNOWN')          AS st,
    COALESCE(s.service_type, 'Unclassified')       AS svc,
    -- Band from the frequency itself. gold.band_classification starts at HF
    -- and so files a 9 kHz assignment under HF; the ITU boundaries are a rule
    -- this export can apply correctly in one place.
    CASE
        WHEN s.frequency_low_hz <        30000 THEN 'VLF'
        WHEN s.frequency_low_hz <       300000 THEN 'LF'
        WHEN s.frequency_low_hz <      3000000 THEN 'MF'
        WHEN s.frequency_low_hz <     30000000 THEN 'HF'
        WHEN s.frequency_low_hz <    300000000 THEN 'VHF'
        WHEN s.frequency_low_hz <   3000000000 THEN 'UHF'
        WHEN s.frequency_low_hz <  30000000000 THEN 'SHF'
        ELSE 'EHF'
    END                                            AS band,
    CAST(ROUND(s.frequency_low_hz  / 1000.0) AS BIGINT) AS lo,
    CAST(ROUND(s.frequency_high_hz / 1000.0) AS BIGINT) AS hi,
    COALESCE(CAST(f.expiry_date AS VARCHAR), '')   AS exp
FROM core_facts.fact_spectrum_allocation s
-- fact_licence.status_key points every row at the CANCELLED dimension row, so
-- anything that resolves status through dim_status reports the whole register
-- as cancelled. The varchar the fact carries alongside it is correct; this
-- reads that, and the warehouse bug is filed rather than inherited.
LEFT JOIN core_facts.fact_licence f
       ON f.licence_id = s.licence_id AND f.is_current
LEFT JOIN dimensions.dim_client c ON c.client_key = s.client_key
WHERE s.is_current
  AND s.frequency_low_hz IS NOT NULL
ORDER BY s.frequency_low_hz
"""

# One row per licence x location x direction. fact_location carries duplicate
# rows at this grain (a known warehouse finding), so it is collapsed here
# rather than shipped four times over.
SITES_SQL = """
SELECT
    f.licence_id                                       AS id,
    CASE WHEN UPPER(f.location_type) LIKE 'R%' THEN 1 ELSE 0 END AS dir,
    COALESCE(d.location_name, '')                      AS loc,
    CAST(ROUND(d.latitude  * 100000) AS BIGINT)        AS lat,
    CAST(ROUND(d.longitude * 100000) AS BIGINT)        AS lon,
    CAST(ROUND(COALESCE(MAX(f.antenna_height_m), -1))  AS INTEGER) AS ht,
    CAST(ROUND(COALESCE(MAX(f.antenna_gain_dbi), -999)) AS INTEGER) AS gain,
    CAST(ROUND(COALESCE(MAX(f.azimuth_degrees), -1))   AS INTEGER) AS az,
    COALESCE(MAX(f.antenna_make), '')                  AS amake
FROM core_facts.fact_location f
JOIN dimensions.dim_location d USING (location_key)
WHERE f.is_current
  AND d.latitude BETWEEN -48 AND -33
  AND d.longitude BETWEEN 165 AND 180
GROUP BY 1, 2, 3, 4, 5
"""

# What of PRISM this rebuild actually covers, measured rather than asserted.
# Spectrum Search Lite shipped sixteen tables; these are the ones the live API
# carries, and the count is the gold row count standing behind each.
PRISM_TABLES = [
    "licence", "clientname", "licencetype", "transmitconfiguration",
    "receiveconfiguration", "spectrum", "emission", "emissionlimit",
    "location", "associatedlicences", "mapdistrict", "radiationpattern",
]


def columnar(rows: list[tuple], names: list[str], text: set[str]) -> dict:
    """Transpose to columns, dictionary-encoding the named text ones."""
    out: dict[str, object] = {}
    dicts: dict[str, list[str]] = {}
    for i, name in enumerate(names):
        col = [r[i] for r in rows]
        if name in text:
            seen: dict[str, int] = {}
            values: list[str] = []
            codes: list[int] = []
            for v in col:
                v = "" if v is None else str(v)
                j = seen.get(v)
                if j is None:
                    j = len(values)
                    seen[v] = j
                    values.append(v)
                codes.append(j)
            dicts[name] = values
            out[name] = codes
        else:
            out[name] = col
    out["dict"] = dicts
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--gold", type=Path, default=GOLD)
    ap.add_argument("--out", type=Path, default=Path(__file__).resolve().parent.parent / "silver")
    args = ap.parse_args()

    if not args.gold.exists():
        sys.exit(f"gold warehouse not found at {args.gold}")
    args.out.mkdir(parents=True, exist_ok=True)

    try:
        conn = duckdb.connect(str(args.gold), read_only=True)
    except duckdb.IOException as exc:
        sys.exit(f"cannot open gold read-only — a build is holding the write lock:\n  {exc}")

    generated = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    spec_names = ["id", "num", "lic", "lt", "st", "svc", "band", "lo", "hi", "exp"]
    spec_rows = conn.execute(SPECTRUM_SQL).fetchall()
    spectrum = columnar(spec_rows, spec_names, {"num", "lic", "lt", "st", "svc", "band", "exp"})
    spectrum["count"] = len(spec_rows)
    spectrum["generatedAt"] = generated

    site_names = ["id", "dir", "loc", "lat", "lon", "ht", "gain", "az", "amake"]
    site_rows = conn.execute(SITES_SQL).fetchall()
    sites = columnar(site_rows, site_names, {"loc", "amake"})
    sites["count"] = len(site_rows)
    sites["generatedAt"] = generated

    # Provenance: which PRISM tables this rebuild stands on, and how much is in
    # each. A missing table reports zero rather than being left out, so a gap
    # is visible on the page instead of only in this file.
    coverage = {}
    for table in PRISM_TABLES:
        try:
            coverage[table] = conn.execute(f"SELECT COUNT(*) FROM staging.{table}").fetchone()[0]
        except duckdb.Error:
            coverage[table] = 0
    # 20% of licences carry no location fact and a further 3% carry one with no
    # coordinate, so a selection landing on an empty map is routine rather than
    # a fault. The page says which it is, and needs the figure to say it.
    mappable = conn.execute("""
        SELECT COUNT(DISTINCT s.licence_id) FROM core_facts.fact_spectrum_allocation s
        WHERE s.is_current AND EXISTS (
            SELECT 1 FROM core_facts.fact_location f
            JOIN dimensions.dim_location d USING (location_key)
            WHERE f.licence_id = s.licence_id AND f.is_current
              AND d.latitude BETWEEN -48 AND -33 AND d.longitude BETWEEN 165 AND 180)
    """).fetchone()[0]
    licensed = conn.execute("""
        SELECT COUNT(DISTINCT licence_id) FROM core_facts.fact_spectrum_allocation
        WHERE is_current""").fetchone()[0]

    meta = {
        "generatedAt": generated,
        "mappableLicences": mappable,
        "spectrumLicences": licensed,
        "source": str(args.gold),
        "prismCoverage": coverage,
        "spectrumRows": len(spec_rows),
        "siteRows": len(site_rows),
        "licences": conn.execute(
            "SELECT COUNT(*) FROM core_facts.fact_licence WHERE is_current").fetchone()[0],
        "holders": len(spectrum["dict"]["lic"]),
    }
    conn.close()

    for name, payload in (("analyser-index.json", spectrum),
                          ("analyser-sites.json", sites),
                          ("prism-meta.json", meta)):
        path = args.out / name
        path.write_text(json.dumps(payload, separators=(",", ":")))
        print(f"{path}  {path.stat().st_size / 1e6:.2f} MB")

    print(f"\nspectrum rows {len(spec_rows):,}   site rows {len(site_rows):,}   "
          f"holders {meta['holders']:,}")
    print("PRISM tables behind it: " +
          ", ".join(f"{k} {v:,}" for k, v in coverage.items() if v))
    absent = [k for k, v in coverage.items() if not v]
    if absent:
        print("empty in gold: " + ", ".join(absent))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
