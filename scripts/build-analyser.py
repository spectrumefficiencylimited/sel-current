#!/usr/bin/env python3
"""Embed a fallback copy of the register files into the analyser page.

The analyser reads the register LIVE — it fetches these same files on load, on
REFRESH and on a timer, so it tracks the hourly pipeline like the rest of the
site. What this script embeds is only a FALLBACK, used when a fetch cannot
succeed: a copy saved to disk and opened over file://, or the site being
unreachable. The page says which of the two it is showing.

The embed is the raw file text, not a pre-computed model, so the live path and
the fallback path run through the same parser and the same renderer in the
page. A shape computed here could drift from the shape computed there; raw text
cannot.

Reads templates/analyser.html, substitutes __SNAPSHOT__, writes analyser.html.
The built file is not committed; it is regenerated every run.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

# Must match the FILES list in the template.
FILES = [
    "silver/stats.json",
    "silver/band_summary.csv",
    "silver/service_summary.csv",
    "silver/band_service_matrix.csv",
    "silver/sample_assignments.csv",
    "silver/licensee_analytics.csv",
    "silver/site_plot.csv",
    "gold/pair_leg_daily.csv",
]


def main() -> int:
    root = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(".")
    template = root / "templates" / "analyser.html"
    output = root / "analyser.html"

    if not template.exists():
        print(f"error: {template} not found", file=sys.stderr)
        return 1

    embedded: dict[str, str] = {}
    missing: list[str] = []
    for rel in FILES:
        path = root / rel
        if path.exists():
            embedded[rel] = path.read_text()
        else:
            missing.append(rel)

    if missing:
        # A missing file is not fatal — the live fetch may still serve it — but
        # the offline fallback would be incomplete, so say so loudly.
        print(f"warning: no fallback copy for {', '.join(missing)}", file=sys.stderr)

    html = template.read_text()
    if "__SNAPSHOT__" not in html:
        print("error: template has no __SNAPSHOT__ placeholder", file=sys.stderr)
        return 1

    blob = json.dumps(embedded, separators=(",", ":"))
    # </script> inside a JSON string would close the host script element early.
    blob = blob.replace("</", "<\\/")
    output.write_text(html.replace("__SNAPSHOT__", blob))

    print(f"built {output} ({output.stat().st_size:,} bytes; "
          f"live fetch + {len(embedded)}-file offline fallback)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
