#!/usr/bin/env bash
# Installs the DuckDB CLI into $DUCKDB_DEST.
#
# The GitHub releases CDN is the single most common cause of failed runs in this
# pipeline (wget "No data received" -> exit 4 -> the whole job dies before any
# data is fetched). This script therefore:
#   1. skips the download entirely when a usable binary is already present
#      (the workflow restores one from the Actions cache),
#   2. retries the download with curl's own retry logic plus an outer backoff,
#   3. falls back to the DuckDB PyPI wheel, which is served by a completely
#      different CDN, and installs a small CLI shim around it.
set -Eeuo pipefail

VERSION="${DUCKDB_VERSION:-v1.0.0}"
DEST="${DUCKDB_DEST:-$HOME/.local/bin}"
ATTEMPTS="${DUCKDB_DOWNLOAD_ATTEMPTS:-4}"
# Override to install from an internal mirror instead of GitHub releases.
ZIP_URL="${DUCKDB_ZIP_URL:-https://github.com/duckdb/duckdb/releases/download/${VERSION}/duckdb_cli-linux-amd64.zip}"

log() { printf '%s - install-duckdb: %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$*"; }

# Tells the workflow whether the result is worth caching. Only a real release
# binary is self-contained; the PyPI-backed shim depends on a pip install that
# would not exist on the next runner.
report_source() {
    [ -n "${GITHUB_OUTPUT:-}" ] && printf 'source=%s\n' "$1" >> "$GITHUB_OUTPUT"
    return 0
}

mkdir -p "$DEST"

# A restored-from-cache shim fails this check (its duckdb module is absent on a
# fresh runner), which correctly sends us down the download path below.
if [ -x "$DEST/duckdb" ] && "$DEST/duckdb" -version >/dev/null 2>&1; then
    log "already installed: $("$DEST/duckdb" -version | head -n1)"
    report_source cached
    exit 0
fi

tmp_dir=$(mktemp -d)
trap 'rm -rf "$tmp_dir"' EXIT

for attempt in $(seq 1 "$ATTEMPTS"); do
    log "downloading ${ZIP_URL} (attempt ${attempt}/${ATTEMPTS})"
    if curl --fail --location --silent --show-error \
        --connect-timeout 20 --max-time 300 \
        --retry 3 --retry-delay 5 --retry-all-errors \
        -o "$tmp_dir/duckdb.zip" "$ZIP_URL" \
        && unzip -o -q "$tmp_dir/duckdb.zip" -d "$tmp_dir" \
        && [ -f "$tmp_dir/duckdb" ]; then
        install -m 0755 "$tmp_dir/duckdb" "$DEST/duckdb"
        log "installed $("$DEST/duckdb" -version | head -n1)"
        report_source release
        exit 0
    fi
    backoff=$(( attempt * 10 ))
    log "download failed; retrying in ${backoff}s"
    sleep "$backoff"
done

log "GitHub releases unavailable; falling back to the DuckDB PyPI wheel"
python3 -m pip install --quiet --disable-pip-version-check --user "duckdb==${VERSION#v}"

cat > "$DEST/duckdb" <<'SHIM'
#!/usr/bin/env python3
"""Fallback DuckDB CLI shim backed by the duckdb Python package.

Supports the two invocation styles this pipeline uses:
    duckdb <database> "<sql>"
    duckdb <database> [-noheader] [-list] < script.sql
Statements are split on ';', which is sufficient for the SQL in this repo but
is not a general-purpose SQL parser. This path only runs when the real CLI
cannot be downloaded.
"""
import sys

import duckdb

if any(a in ("-version", "--version") for a in sys.argv[1:]):
    print("v%s (duckdb python shim)" % duckdb.__version__)
    raise SystemExit(0)

args = [a for a in sys.argv[1:] if not a.startswith("-")]
database = args[0] if args else ":memory:"
sql = args[1] if len(args) > 1 else sys.stdin.read()

connection = duckdb.connect(database)
try:
    for statement in (s.strip() for s in sql.split(";")):
        if not statement:
            continue
        result = connection.execute(statement)
        if result.description:
            for row in result.fetchall():
                print("|".join("" if value is None else str(value) for value in row))
finally:
    connection.close()
SHIM
chmod 0755 "$DEST/duckdb"
report_source pypi
log "installed PyPI-backed shim: $(python3 -c 'import duckdb; print(duckdb.__version__)')"
