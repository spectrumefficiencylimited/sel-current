#!/usr/bin/env python3
"""Bake the current register snapshot into the analyser page.

The analyser is a single self-contained file: no stylesheet, font, script or
data fetch from anywhere. That is deliberate. A page that fetches its data by
relative URL stops working the moment someone saves it and opens it from disk —
`file://` blocks the fetch — so the hosted copy and the downloaded copy would
behave differently. Embedding the snapshot means they cannot.

Reads templates/analyser.html, substitutes __SNAPSHOT__, writes analyser.html.
The built file is not committed; it is regenerated every run, which also keeps
a 40 KB page out of the hourly commit churn.
"""
from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

# Short codes for the matrix column heads. The design's columns were region
# codes; this API carries no region, so the second axis is service, abbreviated
# to keep the 9px heads legible.
SERVICE_CODE = {
    "Land Mobile": "LMR",
    "Spectrum Licence": "SPC",
    "Fixed Link": "FXD",
    "Broadcasting": "BCS",
    "Satellite & Space": "SAT",
    "Paging": "PGE",
    "Maritime": "MAR",
    "Aeronautical": "AER",
    "Amateur": "AMR",
    "General User Licence": "GUL",
    "Radiodetermination": "RDT",
    "Telemetry & Telecommand": "TLM",
    "Outside Broadcast": "OBV",
    "Meteorological": "MET",
}


def read_csv(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(newline="") as handle:
        return list(csv.DictReader(handle))


def to_int(value, default=0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def to_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def build_snapshot(silver: Path, gold: Path) -> dict:
    stats = json.loads((silver / "stats.json").read_text()) if (silver / "stats.json").exists() else {}

    bands = []
    for row in read_csv(silver / "band_summary.csv"):
        bands.append({
            "band": row.get("band", ""),
            # "UHF (300 MHz-3 GHz)" -> "UHF"
            "code": (row.get("band") or "").split(" ")[0],
            "count": to_int(row.get("assignment_count")),
            "licences": to_int(row.get("distinct_licences")),
            "licensees": to_int(row.get("distinct_licensees")),
            "min": to_float(row.get("min_frequency_mhz")),
            "max": to_float(row.get("max_frequency_mhz")),
        })

    services = [
        {
            "service": row.get("service", ""),
            "assignment_count": to_int(row.get("assignment_count")),
            "licensees": to_int(row.get("distinct_licensees")),
        }
        for row in read_csv(silver / "service_summary.csv")
    ]

    # Matrix: bands as rows, the busiest services as columns.
    matrix_rows = read_csv(silver / "band_service_matrix.csv")
    ranked = [s["service"] for s in services][:13]
    counts: dict[tuple[str, str], int] = {}
    band_order: dict[str, int] = {}
    for row in matrix_rows:
        band = row.get("band", "")
        counts[(band, row.get("service", ""))] = to_int(row.get("assignment_count"))
        band_order[band] = to_int(row.get("band_order"), 99)

    def band_label(band: str) -> str:
        """'UHF (300 MHz-3 GHz)' -> 'UHF  300 MHz-3 GHz'."""
        if "(" in band:
            head, _, tail = band.partition("(")
            return f"{head.strip()}  {tail.rstrip(')')}"
        return band

    ordered_bands = sorted(band_order, key=lambda b: band_order[b])
    matrix = {
        "services": [{"code": SERVICE_CODE.get(s, s[:3].upper()), "name": s} for s in ranked],
        "rows": [
            {
                "band": band,
                "label": band_label(band),
                "cells": [counts.get((band, s), 0) for s in ranked],
            }
            for band in ordered_bands
        ],
    }

    sample = [
        {
            "licenceId": row.get("licenceId", ""),
            "licenceNumber": row.get("licenceNumber", ""),
            "licensee": row.get("licensee", ""),
            "channel": row.get("channel", ""),
            "frequency": f"{to_float(row.get('frequency')) or 0:.5f}",
            "location": row.get("location", ""),
            "status": row.get("status", ""),
        }
        for row in read_csv(silver / "sample_assignments.csv")[:10]
    ]

    holders = [
        {"licensee": row.get("licensee", ""), "count": to_int(row.get("assignment_count"))}
        for row in read_csv(silver / "licensee_analytics.csv")[:8]
    ]

    sites = [
        {
            "lat": to_float(row.get("lat")),
            "lon": to_float(row.get("lon")),
            "count": to_int(row.get("assignment_count")),
            "service": row.get("service", ""),
        }
        for row in read_csv(silver / "site_plot.csv")
        if to_float(row.get("lat")) is not None and to_float(row.get("lon")) is not None
    ]

    # Return legs, from the latest day in the pair-leg series.
    return_legs = 0
    pair_rows = read_csv(gold / "pair_leg_daily.csv")
    if pair_rows:
        latest = max(r.get("observed_date", "") for r in pair_rows)
        return_legs = sum(
            to_int(r.get("assignment_count"))
            for r in pair_rows
            if r.get("observed_date") == latest and "Return" in (r.get("pair_leg") or "")
        )

    return {
        "stats": stats,
        "bands": bands,
        "services": services,
        "matrix": matrix,
        "sample": sample,
        "holders": holders,
        "sites": sites,
        "returnLegs": return_legs,
    }


def main() -> int:
    root = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(".")
    template = root / "templates" / "analyser.html"
    output = root / "analyser.html"

    if not template.exists():
        print(f"error: {template} not found", file=sys.stderr)
        return 1

    snapshot = build_snapshot(root / "silver", root / "gold")
    html = template.read_text()
    if "__SNAPSHOT__" not in html:
        print("error: template has no __SNAPSHOT__ placeholder", file=sys.stderr)
        return 1

    # separators without spaces keeps the embedded blob compact
    blob = json.dumps(snapshot, separators=(",", ":"))
    # </script> inside a JSON string would close the host script element early.
    blob = blob.replace("</", "<\\/")
    output.write_text(html.replace("__SNAPSHOT__", blob))

    print(f"built {output} ({output.stat().st_size:,} bytes, "
          f"{len(snapshot['sites'])} sites, {len(snapshot['bands'])} bands, "
          f"{len(snapshot['matrix']['rows'])}x{len(snapshot['matrix']['services'])} matrix)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
