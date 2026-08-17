#!/usr/bin/env python3
"""Mine the summary files out of git history into a compact time series.

The repository holds ~8,400 hourly commits. Each one carries a full regenerated
dataset (~80 MB) that is worthless as history — the point of this project is the
*current* data at a stable URL — but the small summary files inside those same
commits are a genuine 13 month record of how the register has moved.

This extracts those summaries so they survive history truncation.

It never needs the large blobs, so it runs against a partial clone:

    git clone --filter=blob:limit=10k --no-checkout --single-branch \\
        --branch main https://github.com/spectrumefficiencylimited/sel-current.git hist
    ./scripts/backfill-history.py --repo hist --output gold/

That clone is a few megabytes rather than 10.6 GB, because a blob filter fetches
only the small files this script reads.
"""
from __future__ import annotations

import argparse
import csv
import json
import subprocess
import sys
from collections import OrderedDict
from pathlib import Path

STATS_PATH = "silver/stats.json"
ANALYTICS_PATH = "silver/licensee_analytics.csv"


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True, capture_output=True, text=True,
    ).stdout


def commits_touching(repo: Path, branch: str, path: str) -> list[tuple[str, str]]:
    """(sha, committer date) for every commit that changed `path`, oldest first.

    --full-history is load-bearing. Path-limited `git log` simplifies history: at
    a merge whose tree matches one parent for this path, it follows that parent
    and prunes the other side entirely. Resolving a conflict on stats.json in
    favour of one branch therefore hides every observation on the other — here it
    silently dropped five days, and the run looked successful. Duplicates from
    walking both sides are harmless: rows are keyed by timestamp below.
    """
    out = git(repo, "log", "--reverse", "--full-history", "--format=%H\t%cI", branch, "--", path)
    return [tuple(line.split("\t", 1)) for line in out.splitlines() if line.strip()]


def read_blobs(repo: Path, revs: list[str], path: str) -> list[bytes | None]:
    """Read `path` at many revisions in one git process.

    One `git show` per commit would be ~8,400 process spawns; `cat-file --batch`
    streams them all through a single pipe.
    """
    request = "".join(f"{rev}:{path}\n" for rev in revs).encode()
    proc = subprocess.run(
        ["git", "-C", str(repo), "cat-file", "--batch"],
        input=request, capture_output=True, check=True,
    )
    payloads: list[bytes | None] = []
    buf, pos = proc.stdout, 0
    while pos < len(buf):
        eol = buf.index(b"\n", pos)
        header = buf[pos:eol].decode()
        pos = eol + 1
        if header.endswith(("missing", "ambiguous")):
            payloads.append(None)
            continue
        size = int(header.rsplit(" ", 1)[1])
        payloads.append(buf[pos:pos + size])
        pos += size + 1  # trailing newline after each payload
    return payloads


def build_totals(repo: Path, branch: str) -> list[dict]:
    entries = commits_touching(repo, branch, STATS_PATH)
    if not entries:
        return []
    blobs = read_blobs(repo, [sha for sha, _ in entries], STATS_PATH)

    # Keyed by timestamp so re-running, or overlapping histories, cannot produce
    # duplicate observations.
    rows: "OrderedDict[str, dict]" = OrderedDict()
    for (sha, commit_date), blob in zip(entries, blobs):
        if not blob:
            continue
        try:
            stats = json.loads(blob)
        except json.JSONDecodeError:
            print(f"  skipping unparseable stats.json at {sha[:9]}", file=sys.stderr)
            continue
        # lastUpdateUTC is when the data was fetched; the commit date is merely
        # when it landed. Prefer the former, fall back to the latter.
        observed = stats.get("lastUpdateUTC") or commit_date
        total = stats.get("totalLicenses")
        holders = stats.get("uniqueHolders")
        if total is None:
            continue
        rows[observed] = {
            "observed_at": observed,
            "total_licences": total,
            "unique_holders": holders if holders is not None else "",
        }
    return sorted(rows.values(), key=lambda r: r["observed_at"])


def build_licensees(repo: Path, branch: str) -> list[dict]:
    entries = commits_touching(repo, branch, ANALYTICS_PATH)
    if not entries:
        return []
    blobs = read_blobs(repo, [sha for sha, _ in entries], ANALYTICS_PATH)

    # The top-25 table changes rarely, so many commits share a value. Collapse to
    # one observation per UTC day, keeping the last of that day.
    by_day: "OrderedDict[str, list[dict]]" = OrderedDict()
    for (sha, commit_date), blob in zip(entries, blobs):
        if not blob:
            continue
        day = commit_date[:10]
        try:
            reader = csv.DictReader(blob.decode("utf-8", "replace").splitlines())
            day_rows = []
            for rank, record in enumerate(reader, start=1):
                licensee = (record.get("licensee") or "").strip()
                count = (record.get("assignment_count") or "").strip()
                if not licensee or not count:
                    continue
                day_rows.append({
                    "observed_date": day,
                    "rank": rank,
                    "licensee": licensee,
                    "assignment_count": count,
                })
        except Exception as exc:  # noqa: BLE001 - a bad historical blob must not stop the mine
            print(f"  skipping unreadable analytics at {sha[:9]}: {exc}", file=sys.stderr)
            continue
        if day_rows:
            by_day[day] = day_rows

    return [row for day in sorted(by_day) for row in by_day[day]]


def write_csv(path: Path, rows: list[dict], columns: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as handle:
        # csv defaults to CRLF. The shell pipeline that maintains these same
        # files emits LF, and its schema-drift guard compares raw header lines —
        # a CRLF header reads as a different schema and aborts every run.
        writer = csv.DictWriter(handle, fieldnames=columns, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--repo", type=Path, default=Path("."),
                        help="repository to mine (default: current directory)")
    parser.add_argument("--branch", default="main")
    parser.add_argument("--output", type=Path, default=Path("gold"),
                        help="directory to write the time series into")
    args = parser.parse_args()

    if not (args.repo / ".git").exists():
        print(f"error: {args.repo} is not a git repository", file=sys.stderr)
        return 1

    print(f"mining {args.branch} in {args.repo}...")

    # Column names match what the pipeline appends from now on, so the mined
    # history and the ongoing series are one continuous dataset.
    totals = build_totals(args.repo, args.branch)
    write_csv(args.output / "totals_history.csv", totals,
              ["observed_at", "total_licences", "unique_holders"])
    print(f"  totals_history.csv: {len(totals)} observations"
          + (f" ({totals[0]['observed_at'][:10]} to {totals[-1]['observed_at'][:10]})" if totals else ""))

    licensees = build_licensees(args.repo, args.branch)
    write_csv(args.output / "licensee_daily.csv", licensees,
              ["observed_date", "rank", "licensee", "assignment_count"])
    days = len({row["observed_date"] for row in licensees})
    print(f"  licensee_daily.csv: {len(licensees)} rows across {days} days")
    print("\nNote: the top-25 table only changed on the days present here; a gap")
    print("means the ranking was unchanged from the previous observation.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
