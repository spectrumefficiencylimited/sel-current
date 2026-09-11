#!/usr/bin/env python3
"""Fold the current register into the compact payload analyser.html reads.

`analyser.html` is a spectrum analyser view of the same data the portal shows —
occupancy by band and service, the transmit site plot, the top holders, a
tuneable frequency window. Computing any of that in the browser would mean
pulling the 34 MB snapshot down on every visit, so the aggregation happens here
and the page fetches one ~100 KB file.

The classification is a line-for-line port of `sql/enrich.sql`. The two are
copies of one rule and will drift if nobody watches them: `service` must never
come out as `Unclassified`, and this script exits non-zero if it does, so a new
RSM licence type surfaces as a failed run rather than as a silently wrong chart.

`silver/analyser.json` is a build output, not history. It is regenerated every
run and published to the site; it is deliberately not committed, for the same
reason the multi-megabyte datasets are not.

    ./scripts/build-analyser-payload.py
    ./scripts/build-analyser-payload.py --repo-root . --output silver/analyser.json
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Any

# --- classification: a line-for-line port of sql/enrich.sql -----------------
# Keep these in step with the SQL. Ordering matters in both CASE expressions
# for the same reasons documented there.


def service_of(licence_type: str | None) -> str:
    t = (licence_type or "").lower()
    if "amateur" in t:
        return "Amateur"
    if t.startswith("aero"):
        return "Aeronautical"
    if t.startswith("maritime"):
        return "Maritime"
    if t.startswith("meteorolog"):
        return "Meteorological"
    if t.startswith("satellite") or t.startswith("space operations"):
        return "Satellite & Space"
    if "radar" in t or t.startswith("radiodet") or t.startswith("two frequency"):
        return "Radiodetermination"
    if t.startswith("telemetry"):
        return "Telemetry & Telecommand"
    if t.startswith("paging"):
        return "Paging"
    if t.startswith("fixed television outside"):
        return "Outside Broadcast"
    if t in ("vhf fm", "mf am", "uhf tv") or t.startswith("hf am"):
        return "Broadcasting"
    if t.startswith("fixed"):
        return "Fixed Link"
    if t.startswith("land") or t.startswith("prs"):
        return "Land Mobile"
    if t.startswith("general user licence"):
        return "General User Licence"
    if "(spectrum)" in t or t == "managed spectrum park":
        return "Spectrum Licence"
    return "Unclassified"


def link_mode_of(licence_type: str | None) -> str:
    t = (licence_type or "").lower()
    if "mobile transmit" in t:
        return "Mobile Transmit"
    if "bi-directional" in t:
        return "Bi-directional"
    if "uni-directional" in t:
        return "Uni-directional"
    if "simplex" in t:
        return "Simplex"
    if "repeater" in t or "digipeater" in t:
        return "Repeater"
    if "base" in t:
        return "Base Station"
    if "beacon" in t:
        return "Beacon"
    return "Unspecified"


BANDS: list[tuple[float, str, int]] = [
    (0.03, "VLF (<30 kHz)", 1),
    (0.3, "LF (30-300 kHz)", 2),
    (3.0, "MF (300 kHz-3 MHz)", 3),
    (30.0, "HF (3-30 MHz)", 4),
    (300.0, "VHF (30-300 MHz)", 5),
    (3000.0, "UHF (300 MHz-3 GHz)", 6),
    (30000.0, "SHF (3-30 GHz)", 7),
]


def band_of(freq: float | None) -> tuple[str, int]:
    if freq is None:
        return "Unknown", 99
    for ceiling, name, order in BANDS:
        if freq < ceiling:
            return name, order
    return "EHF (>30 GHz)", 8


def pair_leg_of(channel: str | None) -> str:
    return "Return leg (#)" if (channel or "").endswith("#") else "Primary leg"


# --- the frequency ranges the occupancy matrix reports on -------------------
# Each is a real slice of the register, chosen to cover the bands NZ actually
# assigns heavily rather than to divide the spectrum evenly.
MATRIX_ROWS: list[tuple[str, float, float]] = [
    ("26 - 88", 26.0, 88.0),
    ("138 - 174", 138.0, 174.0),
    ("400 - 430", 400.0, 430.0),
    ("450 - 520", 450.0, 520.0),
    ("703 - 803", 703.0, 803.0),
    ("1710 - 2170", 1710.0, 2170.0),
    ("3300 - 3800", 3300.0, 3800.0),
    ("17700 - 19700", 17700.0, 19700.0),
    ("21200 - 23600", 21200.0, 23600.0),
]

# The matrix columns are services, not regions: the register carries a service
# classification for every row, whereas a region would have to be invented from
# free-text location names like "ALL NORTH ISLAND" and "MOBILE".
MATRIX_SERVICES: list[str] = [
    "Land Mobile",
    "Fixed Link",
    "Spectrum Licence",
    "Broadcasting",
    "Maritime",
    "Aeronautical",
    "Amateur",
    "Paging",
    "Radiodetermination",
    "Telemetry & Telecommand",
    "Satellite & Space",
    "Outside Broadcast",
    "General User Licence",
]
MATRIX_ABBR: dict[str, str] = {
    "Land Mobile": "LMR",
    "Fixed Link": "FXD",
    "Spectrum Licence": "SPL",
    "Broadcasting": "BCS",
    "Maritime": "MAR",
    "Aeronautical": "AER",
    "Amateur": "AMA",
    "Paging": "PAG",
    "Radiodetermination": "RDT",
    "Telemetry & Telecommand": "TLM",
    "Satellite & Space": "SAT",
    "Outside Broadcast": "OBS",
    "General User Licence": "GUL",
}

# The window the frequency tuning panel opens on: the 400-520 MHz land mobile
# range, which is the busiest part of the register.
TUNE_LOW, TUNE_HIGH, TUNE_STEP_KHZ = 400.0, 520.0, 12.5




# Distinct transmit sites to plot. Beyond this the map is a solid blob.
MAX_SITES = 900


def parse_grid_reference(default_type: str | None, value: str | None) -> tuple[float, float] | None:
    """Return (lat, lon) when the row carries a WGS84/NZGD2000 lat-long, else None.

    Rows use several grid systems; only the lat-long ones are plotted, and the
    rest are dropped rather than converted, so nothing on the map is estimated.
    """
    if not value or not default_type:
        return None
    if "LAT_LONG" not in str(default_type).upper():
        return None
    parts = str(value).replace(",", " ").split()
    if len(parts) < 2:
        return None
    try:
        lat, lon = float(parts[0]), float(parts[1])
    except ValueError:
        return None
    if not (-48.5 <= lat <= -33.0 and 165.0 <= lon <= 180.0):
        return None
    return lat, lon


def read_csv(path: str) -> list[dict[str, str]]:
    if not os.path.exists(path):
        return []
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


# --- movement, read out of the gold layer ----------------------------------
# The snapshot says what the register holds right now. The gold series says
# whether that is more or less than it held last week, which is the question a
# count on its own cannot answer. Every breakdown below carries both.


def comparison_dates(dates: list[str], days: int) -> tuple[str, str] | None:
    """Pick the pair of observed days a delta is measured across.

    Prefer the last day at least `days` back. Where the file does not reach that
    far — the per-band, per-service and per-type series only begin 2026-08-12,
    while the top-holder series is back-filled to 2025-07-18 — fall back to the
    earliest day it does have, and let the caller report the span it got. A
    five-day movement labelled as five days is useful; the same number labelled
    as a week is not.
    """
    if len(dates) < 2:
        return None
    latest = dates[-1]
    try:
        target = (date.fromisoformat(latest) - timedelta(days=days)).isoformat()
    except ValueError:
        return None
    earlier = [d for d in dates if d <= target]
    compared = earlier[-1] if earlier else dates[0]
    return (latest, compared) if compared != latest else None


def span_days(latest: str, compared: str) -> int | None:
    try:
        return (date.fromisoformat(latest) - date.fromisoformat(compared)).days
    except ValueError:
        return None


def gold_delta(
    path: str,
    key_col: str,
    days: int = 7,
    value_col: str = "assignment_count",
) -> dict[str, Any]:
    """Change in `value_col` per key, across the widest window up to `days`.

    Returns {deltas, latest, compared, days}. Keys present on only one of the two
    days are omitted rather than reported as a full-size gain or loss — a service
    that first appears this week has no honest delta, and showing its whole count
    as growth would be a lie of arithmetic.

    Files whose grain is finer than `key_col` (service_daily is service x mode)
    are summed down to it.
    """
    empty: dict[str, Any] = {"deltas": {}, "latest": None, "compared": None, "days": None}
    rows = read_csv(path)
    if not rows:
        return empty
    dates = sorted({r["observed_date"] for r in rows if r.get("observed_date")})
    if not dates:
        return empty
    pair = comparison_dates(dates, days)
    if pair is None:
        return {**empty, "latest": dates[-1]}
    latest, compared = pair

    now_totals: dict[str, int] = {}
    then_totals: dict[str, int] = {}
    for r in rows:
        key = r.get(key_col)
        if not key:
            continue
        try:
            value = int(r.get(value_col) or 0)
        except ValueError:
            continue
        if r["observed_date"] == latest:
            now_totals[key] = now_totals.get(key, 0) + value
        elif r["observed_date"] == compared:
            then_totals[key] = then_totals.get(key, 0) + value

    return {
        "deltas": {k: v - then_totals[k] for k, v in now_totals.items() if k in then_totals},
        "latest": latest,
        "compared": compared,
        "days": span_days(latest, compared),
    }


def gold_rank_delta(path: str, key_col: str, days: int = 7) -> dict[str, int]:
    """Movement up or down the ranking over `days`, positive meaning a climb.

    Only in `licensee_daily` and `location_daily`, which carry an explicit rank.
    Rows before the pipeline changeover are top 25 rather than top 100, so a
    holder that was outside the old cut has no comparable rank and is skipped.
    """
    rows = read_csv(path)
    if not rows:
        return {}
    dates = sorted({r["observed_date"] for r in rows if r.get("observed_date")})
    pair = comparison_dates(dates, days)
    if pair is None:
        return {}
    latest, compared = pair

    now_rank: dict[str, int] = {}
    then_rank: dict[str, int] = {}
    for r in rows:
        key, rank = r.get(key_col), r.get("rank")
        if not key or not rank:
            continue
        try:
            value = int(rank)
        except ValueError:
            continue
        if r["observed_date"] == latest:
            now_rank[key] = value
        elif r["observed_date"] == compared:
            then_rank[key] = value

    return {k: then_rank[k] - v for k, v in now_rank.items() if k in then_rank}


def daily_history(rows: list[dict[str, str]]) -> dict[str, Any]:
    """Collapse the run-by-run totals to one point per day, plus the headline moves.

    The full series is ~8,500 runs over thirteen months. Shipping it whole would
    triple the payload to draw a line 400 pixels wide, so it is reduced to the
    last reading of each day — and the deltas are computed against the full
    series first, so they are unaffected by that reduction.
    """
    by_day: dict[str, dict[str, int | None]] = {}
    for r in rows:
        stamp = r.get("observed_at") or ""
        day = stamp[:10]
        if not day:
            continue
        try:
            records = int(r.get("total_licences") or 0)
            holders = int(r.get("unique_holders") or 0)
        except ValueError:
            continue
        licences_raw = r.get("distinct_licences")
        try:
            licences = int(licences_raw) if licences_raw else None
        except ValueError:
            licences = None
        # Later rows for the same day overwrite earlier ones: the file is in
        # chronological order, so this keeps the day's last reading.
        by_day[day] = {"records": records, "holders": holders, "licences": licences}

    days = sorted(by_day)
    if not days:
        return {"days": [], "records": [], "holders": [], "summary": {}}

    latest_day = days[-1]
    latest = by_day[latest_day]

    def change(back: int) -> dict[str, int | None] | None:
        try:
            target = (date.fromisoformat(latest_day) - timedelta(days=back)).isoformat()
        except ValueError:
            return None
        earlier = [d for d in days if d <= target]
        if not earlier:
            return None
        prior = by_day[earlier[-1]]
        return {
            "from": earlier[-1],
            "records": (latest["records"] or 0) - (prior["records"] or 0),
            "holders": (latest["holders"] or 0) - (prior["holders"] or 0),
        }

    return {
        # Parallel arrays rather than a list of objects: same information, and
        # it roughly halves this section of the payload.
        "days": days,
        "records": [by_day[d]["records"] for d in days],
        "holders": [by_day[d]["holders"] for d in days],
        "summary": {
            "latestDay": latest_day,
            "records": latest["records"],
            "holders": latest["holders"],
            "licences": latest["licences"],
            "firstDay": days[0],
            "d1": change(1),
            "d7": change(7),
            "d30": change(30),
        },
    }


def build_index(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Fold the register into a per-assignment index the tuning panel can query.

    The main payload is pre-aggregated, which is what keeps first paint at ~80 KB,
    but it cannot answer "show me 433.2-434.8 MHz, land mobile, current" — that
    needs the individual assignments. So they are shipped separately, in a shape
    chosen to survive the trip: columnar rather than a list of objects (no key
    repeated 85 000 times), dictionary-encoded for the text columns, and sorted
    by frequency so the deltas in `f` are small and gzip has something to chew
    on. Roughly 3.2 MB of JSON that leaves the server as ~640 KB.

    The page fetches this only when someone first touches the tuning controls,
    so a reader who never queries never pays for it.
    """
    rows = [r for r in rows if r.get("frequency") is not None]
    rows.sort(key=lambda r: r["frequency"])

    dicts: dict[str, list[str]] = {}
    cols: dict[str, list[int]] = {}

    def encode(name: str, value_of) -> None:
        seen: dict[str, int] = {}
        values: list[str] = []
        out: list[int] = []
        for r in rows:
            v = value_of(r) or ""
            i = seen.get(v)
            if i is None:
                i = len(values)
                seen[v] = i
                values.append(v)
            out.append(i)
        dicts[name] = values
        cols[name] = out

    encode("lic", lambda r: r.get("licensee"))
    encode("loc", lambda r: r.get("location"))
    encode("lt", lambda r: r.get("licenceType"))
    encode("st", lambda r: r.get("status"))
    encode("tx", lambda r: r.get("txrx"))
    encode("ch", lambda r: r.get("channel"))
    # Service and link mode are derived, not stored. Deriving them here rather
    # than in the browser keeps the one classification rule in one place.
    encode("svc", lambda r: service_of(r.get("licenceType")))
    encode("lm", lambda r: link_mode_of(r.get("licenceType")))

    # Frequency in whole Hz. Stored as first-value-then-deltas over the sorted
    # order: the values span nine orders of magnitude, the deltas do not.
    hz = [int(round(float(r["frequency"]) * 1e6)) for r in rows]
    deltas = [hz[0]] + [hz[i] - hz[i - 1] for i in range(1, len(hz))] if hz else []

    return {
        "count": len(rows),
        "lowHz": hz[0] if hz else 0,
        "highHz": hz[-1] if hz else 0,
        "f": deltas,
        "id": [r.get("licenceID") or 0 for r in rows],
        "num": [r.get("licenceNumber") or 0 for r in rows],
        **cols,
        "dict": dicts,
    }


def main() -> int:
    repo_default = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo-root", default=repo_default, help="repository root (default: the one this script lives in)")
    ap.add_argument("--output", default=None, help="output path (default: <repo-root>/silver/analyser.json)")
    ap.add_argument("--index-output", default=None,
                    help="per-assignment index path (default: alongside --output as analyser-index.json)")
    ap.add_argument(
        "--allow-unclassified",
        action="store_true",
        help="write the payload even if some rows fall through to Unclassified",
    )
    args = ap.parse_args()
    root: str = args.repo_root
    out_path: str = args.output or os.path.join(root, "silver", "analyser.json")

    snapshot_path = os.path.join(root, "silver", "combined_licences.json")
    if not os.path.exists(snapshot_path):
        snapshot_path = os.path.join(root, "bronze", "combined_licences.json")
    if not os.path.exists(snapshot_path):
        print(
            "No register snapshot found. It is a build output and is not committed;\n"
            "fetch it with:\n"
            "  curl -L -o silver/combined_licences.json \\\n"
            "    https://spectrumefficiencylimited.github.io/sel-current/silver/combined_licences.json",
            file=sys.stderr,
        )
        return 1

    print(f"reading {snapshot_path} ...", file=sys.stderr)
    with open(snapshot_path, encoding="utf-8") as fh:
        rows: list[dict[str, Any]] = json.load(fh)
    print(f"  {len(rows):,} assignment records", file=sys.stderr)

    band_counts: Counter[str] = Counter()
    band_holders: defaultdict[str, set[str]] = defaultdict(set)
    band_order: dict[str, int] = {}
    service_counts: Counter[str] = Counter()
    service_holders: defaultdict[str, set[str]] = defaultdict(set)
    licensee_counts: Counter[str] = Counter()
    licensee_licences: defaultdict[str, set[int]] = defaultdict(set)
    matrix: defaultdict[tuple[str, str], int] = defaultdict(int)
    pair_leg_counts: Counter[str] = Counter()
    service_leg_counts: defaultdict[str, Counter[str]] = defaultdict(Counter)
    licence_type_counts: Counter[str] = Counter()
    licence_type_holders: defaultdict[str, set[str]] = defaultdict(set)
    location_counts: Counter[str] = Counter()
    location_holders: defaultdict[str, set[str]] = defaultdict(set)
    service_licences: defaultdict[str, set[int]] = defaultdict(set)
    licences: set[int] = set()
    holders: set[str] = set()
    tuned_hist: list[int] = [0] * 120  # 1 MHz buckets across the tuned window
    tuned_rows: list[dict[str, Any]] = []
    sites: dict[tuple[float, float], int] = {}
    site_service: dict[tuple[float, float], Counter[str]] = {}
    unclassified_types: Counter[str] = Counter()
    spectrum_licence_holders: set[str] = set()
    spectrum_licence_recs = 0

    for r in rows:
        lt: str = r.get("licenceType") or ""
        raw_freq = r.get("frequency")
        try:
            freq: float | None = float(raw_freq) if raw_freq is not None else None
        except (TypeError, ValueError):
            freq = None
        svc = service_of(lt)
        bname, border = band_of(freq)
        lee: str = (r.get("licensee") or "").strip()
        lid = r.get("licenceID")

        if svc == "Unclassified":
            unclassified_types[lt] += 1

        loc: str = (r.get("location") or "").strip()

        band_counts[bname] += 1
        band_order[bname] = border
        licence_type_counts[lt or "(none)"] += 1
        if loc:
            location_counts[loc] += 1
        if lee:
            band_holders[bname].add(lee)
            service_holders[svc].add(lee)
            licensee_counts[lee] += 1
            licence_type_holders[lt or "(none)"].add(lee)
            if loc:
                location_holders[loc].add(lee)
            if lid is not None:
                licensee_licences[lee].add(lid)
            holders.add(lee)
        service_counts[svc] += 1
        leg = pair_leg_of(r.get("channel"))
        pair_leg_counts[leg] += 1
        service_leg_counts[svc][leg] += 1
        if lid is not None:
            licences.add(lid)
            service_licences[svc].add(lid)
        if svc == "Spectrum Licence":
            spectrum_licence_recs += 1
            if lee:
                spectrum_licence_holders.add(lee)

        if freq is not None:
            for label, lo, hi in MATRIX_ROWS:
                if lo <= freq < hi:
                    matrix[(label, svc)] += 1
                    break
            if TUNE_LOW <= freq < TUNE_HIGH:
                tuned_hist[min(119, int(freq - TUNE_LOW))] += 1
                tuned_rows.append(
                    {
                        "licenceId": lid,
                        "licenceNumber": r.get("licenceNumber"),
                        "licensee": lee,
                        "channel": r.get("channel"),
                        "frequency": freq,
                        "location": r.get("location") or "",
                        "status": r.get("status") or "",
                        "service": svc,
                        "linkMode": link_mode_of(lt),
                    }
                )

        ll = parse_grid_reference(r.get("gridRefDefault"), r.get("gridReference"))
        if ll:
            key = (round(ll[0], 2), round(ll[1], 2))
            sites[key] = sites.get(key, 0) + 1
            site_service.setdefault(key, Counter())[svc] += 1

    # `service` must never come out as Unclassified. When RSM introduces a new
    # licence type, fail the run rather than publish a chart that quietly drops
    # it — add the type to sql/enrich.sql and to service_of() above.
    if unclassified_types and not args.allow_unclassified:
        print("\nUNCLASSIFIED LICENCE TYPES — the classification needs updating:", file=sys.stderr)
        for t, n in unclassified_types.most_common():
            print(f"  {n:>7,}  {t!r}", file=sys.stderr)
        print(
            "\nAdd them to the CASE in sql/enrich.sql and to service_of() in this script,\n"
            "or re-run with --allow-unclassified to publish anyway.",
            file=sys.stderr,
        )
        return 2

    # --- result grid: the tuned window, newest licence IDs first ------------
    tuned_rows.sort(key=lambda r: (r["licenceId"] or 0), reverse=True)
    grid = tuned_rows[:12]
    tuned_total = sum(tuned_hist)

    # --- map: the busiest distinct transmit sites --------------------------
    top_sites = sorted(sites.items(), key=lambda kv: kv[1], reverse=True)[:MAX_SITES]
    site_list = [
        {"lat": k[0], "lon": k[1], "n": v, "svc": site_service[k].most_common(1)[0][0]}
        for k, v in top_sites
    ]

    # --- the gold layer: what has moved, and by how much --------------------
    gold = lambda name: os.path.join(root, "gold", name)  # noqa: E731
    history = daily_history(read_csv(gold("totals_history.csv")))

    # Each series is compared across the widest window it can actually cover, up
    # to a week — they do not all start on the same day, so each carries its own
    # span and the page labels the column with it rather than assuming 7.
    band_delta = gold_delta(gold("band_daily.csv"), "band")
    service_delta = gold_delta(gold("service_daily.csv"), "service")
    type_delta = gold_delta(gold("licence_type_daily.csv"), "licence_type")
    location_delta = gold_delta(gold("location_daily.csv"), "location")
    licensee_delta = gold_delta(gold("licensee_daily.csv"), "licensee")
    licensee_rank_d7 = gold_rank_delta(gold("licensee_daily.csv"), "licensee")
    location_rank_d7 = gold_rank_delta(gold("location_daily.csv"), "location")

    band_d7 = band_delta["deltas"]
    service_d7 = service_delta["deltas"]
    type_d7 = type_delta["deltas"]
    location_d7 = location_delta["deltas"]
    licensee_d7 = licensee_delta["deltas"]

    stats_path = os.path.join(root, "silver", "stats.json")
    stats: dict[str, Any] = {}
    if os.path.exists(stats_path):
        with open(stats_path, encoding="utf-8") as fh:
            stats = json.load(fh)

    bands_sorted = sorted(band_counts.items(), key=lambda kv: band_order[kv[0]])
    band_max = max(band_counts.values()) if band_counts else 1

    payload = {
        "generatedAt": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        "source": os.path.relpath(snapshot_path, root),
        "lastUpdateUTC": stats.get("lastUpdateUTC", ""),
        "headline": {
            "assignmentRecords": len(rows),
            "distinctLicences": len(licences),
            "uniqueHolders": len(holders),
            "returnLegs": pair_leg_counts.get("Return leg (#)", 0),
            "unclassified": service_counts.get("Unclassified", 0),
        },
        "bands": [
            {
                "code": name.split(" ")[0],
                "label": name,
                "count": count,
                "holders": len(band_holders[name]),
                "pct": round(100.0 * count / band_max, 1),
                "d7": band_d7.get(name),
            }
            for name, count in bands_sorted
        ],
        # The three breakdowns behind the result-set tabs. Counts come from the
        # snapshot, movement from the gold series — the snapshot cannot say
        # whether a number is rising and the gold layer is not the current truth.
        "services": [
            {
                "service": s,
                "count": c,
                "holders": len(service_holders[s]),
                "licences": len(service_licences[s]),
                "primaryLegs": service_leg_counts[s].get("Primary leg", 0),
                "returnLegs": service_leg_counts[s].get("Return leg (#)", 0),
                "d7": service_d7.get(s),
            }
            for s, c in service_counts.most_common()
        ],
        "licenceTypes": [
            {
                "licenceType": t,
                "service": service_of(t),
                "linkMode": link_mode_of(t),
                "count": c,
                "holders": len(licence_type_holders[t]),
                "d7": type_d7.get(t),
            }
            for t, c in licence_type_counts.most_common(60)
        ],
        "locations": [
            {
                "location": loc,
                "count": c,
                "holders": len(location_holders[loc]),
                "d7": location_d7.get(loc),
                "rankD7": location_rank_d7.get(loc),
            }
            for loc, c in location_counts.most_common(60)
        ],
        "matrix": {
            "rows": [r[0] for r in MATRIX_ROWS],
            "cols": [{"key": s, "abbr": MATRIX_ABBR[s]} for s in MATRIX_SERVICES],
            "cells": [
                [matrix.get((label, svc), 0) for svc in MATRIX_SERVICES]
                for label, _, _ in MATRIX_ROWS
            ],
        },
        "tuning": {
            "low": TUNE_LOW,
            "high": TUNE_HIGH,
            "stepKHz": TUNE_STEP_KHZ,
            "histogram": tuned_hist,
            "total": tuned_total,
        },
        "grid": grid,
        "topLicensees": [
            {
                "licensee": lee,
                "records": n,
                "licences": len(licensee_licences[lee]),
                "d7": licensee_d7.get(lee),
                "rankD7": licensee_rank_d7.get(lee),
            }
            for lee, n in licensee_counts.most_common(8)
        ],
        "spectrumLicence": {
            "records": spectrum_licence_recs,
            "holders": len(spectrum_licence_holders),
        },
        "sites": site_list,
        "history": history,
        # Which two days each delta compares, per series. Shown in the UI so a
        # movement can never be read as covering a week when it covers five days.
        "deltaWindows": {
            "bands": {k: band_delta[k] for k in ("latest", "compared", "days")},
            "services": {k: service_delta[k] for k in ("latest", "compared", "days")},
            "licenceTypes": {k: type_delta[k] for k in ("latest", "compared", "days")},
            "locations": {k: location_delta[k] for k in ("latest", "compared", "days")},
            "licensees": {k: licensee_delta[k] for k in ("latest", "compared", "days")},
        },
    }

    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, separators=(",", ":"))
        fh.write("\n")
    size = os.path.getsize(out_path)
    print(f"wrote {out_path} ({size:,} bytes)", file=sys.stderr)

    index_path = args.index_output or os.path.join(os.path.dirname(out_path), "analyser-index.json")
    index = build_index(rows)
    with open(index_path, "w", encoding="utf-8") as fh:
        json.dump(index, fh, separators=(",", ":"))
        fh.write("\n")
    index_size = os.path.getsize(index_path)
    print(f"wrote {index_path} ({index_size:,} bytes, {index['count']:,} assignments)",
          file=sys.stderr)

    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write("\n### Analyser payload\n\n")
            fh.write("| Metric | Value |\n| --- | --- |\n")
            fh.write(f"| Payload size | {size / 1024:.0f} KB |\n")
            fh.write(f"| Assignment records | {len(rows):,} |\n")
            fh.write(f"| Distinct licences | {len(licences):,} |\n")
            fh.write(f"| Transmit sites plotted | {len(site_list):,} of {len(sites):,} |\n")
            fh.write(f"| Unclassified | {service_counts.get('Unclassified', 0):,} |\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
