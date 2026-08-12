#!/usr/bin/env bash
# Fetches RSM API pages, combines them, and processes into final data assets.
#
# Design goals (see README "Pipeline reliability"):
#   * every network call is bounded by a timeout and retried with backoff
#   * a single flaky page is retried serially instead of killing the whole run
#   * every page is validated (HTTP status + parseable JSON + .items array)
#   * outputs are built in a staging directory and only published atomically
#     once they pass sanity checks, so a bad API response can never overwrite
#     good committed data
# ----------------------------------------------------------------------
set -Eeuo pipefail

# --- CONFIGURATION (all overridable from the workflow) ---
API_BASE="${RSM_API_BASE:-https://api.business.govt.nz/gateway/radio-spectrum-management/v1/licences}"
PAGE_SIZE="${RSM_PAGE_SIZE:-1000}"
PARALLELISM="${RSM_PARALLELISM:-8}"
MAX_ATTEMPTS="${RSM_MAX_ATTEMPTS:-5}"
CONNECT_TIMEOUT="${RSM_CONNECT_TIMEOUT:-20}"
MAX_TIME="${RSM_MAX_TIME:-120}"
MAX_PAGES="${RSM_MAX_PAGES:-500}"

# Data quality gates. A run that would publish fewer than MIN_LICENCES rows, or
# lose more than MAX_DROP_PCT of the previously published rows, aborts before
# touching the committed data. Set ALLOW_DATA_DROP=1 to override deliberately.
MIN_LICENCES="${RSM_MIN_LICENCES:-1000}"
MAX_DROP_PCT="${RSM_MAX_DROP_PCT:-10}"
ALLOW_DATA_DROP="${ALLOW_DATA_DROP:-0}"

BRONZE_DIR="${BRONZE_DIR:-./bronze}"
SILVER_DIR="${SILVER_DIR:-./silver}"
GOLD_DIR="${GOLD_DIR:-./gold}"
PAGES_DIR="${PAGES_DIR:-${BRONZE_DIR}/pages}"

export API_BASE PAGE_SIZE MAX_ATTEMPTS CONNECT_TIMEOUT MAX_TIME PAGES_DIR

# --- LOGGING ---
log() { printf '%s - %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$*"; }
log_orchestrator() { log "ORCHESTRATOR: $*"; }
log_worker() { log "WORKER[$$]: $*"; }
die() { log "FATAL: $*" >&2; exit 1; }

# Report the failing line instead of an anonymous non-zero exit.
# shellcheck disable=SC2154  # rc is assigned inside the trap body
trap 'rc=$?; log "FATAL: ${BASH_SOURCE[0]}:${LINENO} exited with status ${rc}" >&2; exit "$rc"' ERR

# Appends a line to the GitHub Actions job summary when running in CI.
summary() {
    [ -n "${GITHUB_STEP_SUMMARY:-}" ] && printf '%s\n' "$*" >> "$GITHUB_STEP_SUMMARY"
    return 0
}

page_url() {
    local page_num=$1
    printf '%s?page=%s&page-size=%s&sort-by=Licence%%20ID&sort-order=desc&txRx=TRN&licenceStatus=CURRENT&gridRefDefault=LAT_LONG_NZGD2000_D2000' \
        "$API_BASE" "$page_num" "$PAGE_SIZE"
}

# --- API FETCH FUNCTION (WORKER) ---
# Writes $PAGES_DIR/page_N.json only once the response is a 200 carrying an
# `items` array, so a half-written or error-body page can never reach the
# combine step.
fetch_api_page() {
    local page_num=$1
    local output_path="$PAGES_DIR/page_${page_num}.json"
    local url
    url=$(page_url "$page_num")

    local attempt=1
    while [ "$attempt" -le "$MAX_ATTEMPTS" ]; do
        local tmp_file http_code curl_rc backoff
        tmp_file=$(mktemp "$PAGES_DIR/page_${page_num}.tmp.XXXXXX")

        set +e
        http_code=$(curl --location --silent --show-error \
            --connect-timeout "$CONNECT_TIMEOUT" --max-time "$MAX_TIME" \
            -w '%{http_code}' \
            -H "Ocp-Apim-Subscription-Key: $RSM_API_KEY" \
            "$url" -o "$tmp_file")
        curl_rc=$?
        set -e

        # Exponential backoff, capped, so a rate-limited API cannot spin forever.
        backoff=$(( attempt * 5 ))
        [ "$backoff" -gt 60 ] && backoff=60

        if [ "$curl_rc" -ne 0 ]; then
            log_worker "WARN (attempt ${attempt}/${MAX_ATTEMPTS}): curl exit ${curl_rc} on page ${page_num}; retrying in ${backoff}s"
        elif [ "$http_code" = "200" ] && jq -e 'has("items") and (.items | type == "array")' "$tmp_file" >/dev/null 2>&1; then
            mv "$tmp_file" "$output_path"
            log_worker "SUCCESS: page ${page_num} saved ($(jq -r '.items | length' "$output_path") items)."
            return 0
        elif [ "$http_code" = "429" ]; then
            log_worker "RATE LIMITED (attempt ${attempt}/${MAX_ATTEMPTS}) on page ${page_num}; sleeping ${backoff}s"
        elif [ "$http_code" = "200" ]; then
            log_worker "WARN (attempt ${attempt}/${MAX_ATTEMPTS}): page ${page_num} returned 200 but no usable items array; retrying in ${backoff}s"
        else
            log_worker "WARN (attempt ${attempt}/${MAX_ATTEMPTS}): HTTP ${http_code} on page ${page_num}; retrying in ${backoff}s"
        fi

        rm -f "$tmp_file"
        attempt=$(( attempt + 1 ))
        [ "$attempt" -le "$MAX_ATTEMPTS" ] && sleep "$backoff"
    done

    log_worker "FAILED: page ${page_num} after ${MAX_ATTEMPTS} attempts."
    return 1
}

# --- SCRIPT ENTRY POINT (worker invocation from xargs) ---
if [ "${1:-}" = "--fetch-page" ]; then
    [ -n "${2:-}" ] || die "--fetch-page requires a page number"
    # A worker returning non-zero is an expected, recoverable outcome: the
    # orchestrator retries the gap serially. Don't dress it up as a crash.
    trap - ERR
    fetch_api_page "$2"
    exit $?
fi

# --- ORCHESTRATOR MODE ---
[ -n "${RSM_API_KEY:-}" ] || die "RSM_API_KEY environment variable is not set."
command -v jq >/dev/null || die "jq is not installed."
command -v duckdb >/dev/null || die "duckdb is not installed."

mkdir -p "$PAGES_DIR" "$SILVER_DIR"
rm -f "$PAGES_DIR"/page_*.json "$PAGES_DIR"/page_*.tmp.*

WORK_DIR=$(mktemp -d "${TMPDIR:-/tmp}/rsm-build.XXXXXX")
cleanup() { rm -rf "$WORK_DIR"; }
trap cleanup EXIT

log_orchestrator "Starting data fetch and process..."

# 1. FETCHING DATA
log_orchestrator "Fetching page 1 to determine total pages..."
fetch_api_page 1 || die "Could not fetch the first page. The API or the API key is unavailable."

TOTAL_PAGES=$(jq -r '.totalPages // 1' "$PAGES_DIR/page_1.json")
[[ "$TOTAL_PAGES" =~ ^[0-9]+$ ]] || die "Invalid totalPages returned by the API: '${TOTAL_PAGES}'"
[ "$TOTAL_PAGES" -ge 1 ] || die "API reported ${TOTAL_PAGES} pages."
[ "$TOTAL_PAGES" -le "$MAX_PAGES" ] || die "API reported ${TOTAL_PAGES} pages, above the ${MAX_PAGES} guard rail. Refusing to run."
API_TOTAL_ITEMS=$(jq -r '(.totalItems // .totalCount // .total // .totalRecords // empty) | tostring' "$PAGES_DIR/page_1.json")
log_orchestrator "Total pages to fetch: ${TOTAL_PAGES}${API_TOTAL_ITEMS:+ (API reports ${API_TOTAL_ITEMS} items)}"

if [ "$TOTAL_PAGES" -gt 1 ]; then
    log_orchestrator "Fetching pages 2..${TOTAL_PAGES} with ${PARALLELISM} workers..."
    # xargs exits non-zero when any worker fails; that is expected and handled
    # by the gap-filling pass below rather than aborting the whole run.
    seq 2 "$TOTAL_PAGES" | xargs -P "$PARALLELISM" -I{} bash "$0" --fetch-page {} || true

    missing=()
    for page in $(seq 2 "$TOTAL_PAGES"); do
        [ -s "$PAGES_DIR/page_${page}.json" ] || missing+=("$page")
    done

    if [ "${#missing[@]}" -gt 0 ]; then
        log_orchestrator "Retrying ${#missing[@]} missing page(s) serially: ${missing[*]}"
        still_missing=()
        for page in "${missing[@]}"; do
            fetch_api_page "$page" || still_missing+=("$page")
        done
        [ "${#still_missing[@]}" -eq 0 ] || die "Could not fetch page(s): ${still_missing[*]}. Refusing to publish an incomplete dataset."
    fi
fi

rm -f "$PAGES_DIR"/page_*.tmp.*

# 2. COMBINING DATA (BRONZE LAYER)
log_orchestrator "Combining ${TOTAL_PAGES} page(s) into a single document..."
jq -s '[.[].items] | add // []' "$PAGES_DIR"/page_*.json > "$WORK_DIR/combined_licences.json"

TOTAL_LICENSES=$(jq 'length' "$WORK_DIR/combined_licences.json")
[[ "$TOTAL_LICENSES" =~ ^[0-9]+$ ]] || die "Could not count records in the combined document."

# 3. DATA QUALITY GATES (before anything is published)
[ "$TOTAL_LICENSES" -ge "$MIN_LICENCES" ] || die "Only ${TOTAL_LICENSES} records fetched, below the ${MIN_LICENCES} floor. Refusing to publish."

PREVIOUS_TOTAL=0
if [ -f "$SILVER_DIR/stats.json" ]; then
    PREVIOUS_TOTAL=$(jq -r '.totalLicenses // 0' "$SILVER_DIR/stats.json" 2>/dev/null || echo 0)
    [[ "$PREVIOUS_TOTAL" =~ ^[0-9]+$ ]] || PREVIOUS_TOTAL=0
fi

if [ "$PREVIOUS_TOTAL" -gt 0 ]; then
    FLOOR=$(( PREVIOUS_TOTAL * (100 - MAX_DROP_PCT) / 100 ))
    if [ "$TOTAL_LICENSES" -lt "$FLOOR" ]; then
        if [ "$ALLOW_DATA_DROP" = "1" ]; then
            log_orchestrator "WARN: record count dropped ${PREVIOUS_TOTAL} -> ${TOTAL_LICENSES}; publishing anyway (ALLOW_DATA_DROP=1)."
        else
            die "Record count dropped ${PREVIOUS_TOTAL} -> ${TOTAL_LICENSES} (more than ${MAX_DROP_PCT}%). Refusing to publish. Re-run with ALLOW_DATA_DROP=1 if this is a genuine change."
        fi
    fi
fi

if [ -n "$API_TOTAL_ITEMS" ] && [[ "$API_TOTAL_ITEMS" =~ ^[0-9]+$ ]] && [ "$API_TOTAL_ITEMS" -ne "$TOTAL_LICENSES" ]; then
    log_orchestrator "NOTE: API advertised ${API_TOTAL_ITEMS} items, collected ${TOTAL_LICENSES} (the register changes while we paginate)."
fi

DUPLICATE_IDS=$(jq '[.[].licenceID] | length - (unique | length)' "$WORK_DIR/combined_licences.json")
if [ "$DUPLICATE_IDS" -gt 0 ]; then
    log_orchestrator "NOTE: ${DUPLICATE_IDS} duplicate licenceID(s) present (records shifting between pages during pagination)."
fi

# 4. PROCESSING DATA (SILVER LAYER), still in staging
log_orchestrator "Processing ${TOTAL_LICENSES} records into Silver layer assets..."

log_orchestrator "Generating CSV file..."
echo "licenceId,licenceNumber,licensee,channel,frequency,location,status,txrx,suppressed" > "$WORK_DIR/combined_licences.csv"
jq -r '.[] | [.licenceID, .licenceNumber, .licensee, .channel, .frequency, .location, .status, .txrx, .suppressed] | @csv' \
    "$WORK_DIR/combined_licences.json" >> "$WORK_DIR/combined_licences.csv"

# A small sample for the web page's preview table. Without this the page has to
# download the full multi-megabyte CSV just to render ten rows, which is painful
# on a phone.
log_orchestrator "Generating preview sample..."
head -n 11 "$WORK_DIR/combined_licences.csv" > "$WORK_DIR/sample_assignments.csv"

log_orchestrator "Generating DuckDB file..."
duckdb "$WORK_DIR/combined_licences.duckdb" \
    "CREATE OR REPLACE TABLE licences AS SELECT * FROM read_csv_auto('$WORK_DIR/combined_licences.csv', ALL_VARCHAR=TRUE);"

log_orchestrator "Generating licensee analytics..."
duckdb "$WORK_DIR/combined_licences.duckdb" <<EOF
COPY (
    SELECT
        licensee,
        COUNT(*) AS assignment_count
    FROM licences
    WHERE location != 'MOBILE'
    GROUP BY licensee
    ORDER BY assignment_count DESC
    LIMIT 25
) TO '$WORK_DIR/licensee_analytics.csv' (HEADER, DELIMITER ',');
EOF

UNIQUE_HOLDERS=$(jq '[.[] | .licensee] | unique | length' "$WORK_DIR/combined_licences.json")
cat > "$WORK_DIR/stats.json" << EOF
{
  "totalLicenses": ${TOTAL_LICENSES},
  "activeAssignments": ${TOTAL_LICENSES},
  "uniqueHolders": ${UNIQUE_HOLDERS},
  "lastUpdateUTC": "$(date -u --iso-8601=seconds)"
}
EOF

# 4b. GOLD LAYER: small aggregates that accumulate over time
#
# The raw snapshot is deliberately not kept as history — the point of this
# project is the *current* dataset at a stable URL. What is worth keeping is how
# the register moves, so each run folds the current snapshot into a compact time
# series. A year of these is a few megabytes.
#
# Aggregates are built from a temporary CSV carrying every field, including
# licenceType and gridReference, which the published CSV does not expose. The
# published schema is a stable contract for downstream dashboards and is left
# exactly as it is.
log_orchestrator "Building gold layer aggregates..."

echo "licenceId,licenceNumber,licensee,channel,frequency,location,licenceType,gridReference,status,txrx,suppressed" > "$WORK_DIR/analytics_input.csv"
jq -r '.[] | [.licenceID, .licenceNumber, .licensee, .channel, .frequency, .location, .licenceType, .gridReference, .status, .txrx, .suppressed] | @csv' \
    "$WORK_DIR/combined_licences.json" >> "$WORK_DIR/analytics_input.csv"

OBSERVED_AT=$(jq -r '.lastUpdateUTC' "$WORK_DIR/stats.json")
OBSERVED_DATE="${OBSERVED_AT:0:10}"

duckdb "$WORK_DIR/gold.duckdb" <<EOF
CREATE OR REPLACE TABLE licences AS
    SELECT * FROM read_csv_auto('$WORK_DIR/analytics_input.csv', ALL_VARCHAR=TRUE);

COPY (
    SELECT
        '${OBSERVED_DATE}' AS observed_date,
        ROW_NUMBER() OVER (ORDER BY COUNT(*) DESC, licensee) AS rank,
        licensee,
        COUNT(*) AS assignment_count
    FROM licences
    WHERE location IS DISTINCT FROM 'MOBILE'
    GROUP BY licensee
    ORDER BY assignment_count DESC, licensee
    LIMIT ${GOLD_TOP_N:-100}
) TO '$WORK_DIR/licensee_daily_new.csv' (HEADER, DELIMITER ',');

COPY (
    SELECT
        '${OBSERVED_DATE}' AS observed_date,
        ROW_NUMBER() OVER (ORDER BY COUNT(*) DESC, location) AS rank,
        location,
        COUNT(*) AS assignment_count,
        COUNT(DISTINCT licensee) AS distinct_licensees
    FROM licences
    WHERE location IS DISTINCT FROM 'MOBILE' AND location IS NOT NULL
    GROUP BY location
    ORDER BY assignment_count DESC, location
    LIMIT ${GOLD_TOP_N:-100}
) TO '$WORK_DIR/location_daily_new.csv' (HEADER, DELIMITER ',');

COPY (
    SELECT
        '${OBSERVED_DATE}' AS observed_date,
        licence_type,
        assignment_count,
        distinct_licensees
    FROM (
        SELECT
            COALESCE(licenceType, 'UNKNOWN') AS licence_type,
            COUNT(*) AS assignment_count,
            COUNT(DISTINCT licensee) AS distinct_licensees
        FROM licences
        GROUP BY 1
    )
    ORDER BY assignment_count DESC, licence_type
) TO '$WORK_DIR/licence_type_daily_new.csv' (HEADER, DELIMITER ',');
EOF

mkdir -p "$GOLD_DIR"

# Replaces any rows already recorded for this date, so re-running within a day
# refreshes that day rather than duplicating it. observed_date is the first
# column, which makes the anchored match unambiguous.
upsert_daily() {
    local target="$1" incoming="$2" tmp
    tmp=$(mktemp "${WORK_DIR}/upsert.XXXXXX")
    if [ -f "$target" ]; then
        head -n 1 "$target" > "$tmp"
        tail -n +2 "$target" | { grep -v "^${OBSERVED_DATE}," || true; } >> "$tmp"
    else
        head -n 1 "$incoming" > "$tmp"
    fi
    tail -n +2 "$incoming" >> "$tmp"
    # mktemp creates 0600; these are published files.
    install -m 0644 "$tmp" "$target"
    rm -f "$tmp"
}

upsert_daily "$GOLD_DIR/licensee_daily.csv" "$WORK_DIR/licensee_daily_new.csv"
upsert_daily "$GOLD_DIR/location_daily.csv" "$WORK_DIR/location_daily_new.csv"
upsert_daily "$GOLD_DIR/licence_type_daily.csv" "$WORK_DIR/licence_type_daily_new.csv"

# The totals series is one row per run, not per day: it is the finest-grained
# and cheapest signal, at roughly 500 KB a year.
if [ ! -f "$GOLD_DIR/totals_history.csv" ]; then
    echo "observed_at,total_licences,unique_holders" > "$GOLD_DIR/totals_history.csv"
fi
if ! grep -q "^${OBSERVED_AT}," "$GOLD_DIR/totals_history.csv"; then
    echo "${OBSERVED_AT},${TOTAL_LICENSES},${UNIQUE_HOLDERS}" >> "$GOLD_DIR/totals_history.csv"
fi

log_orchestrator "Gold layer updated for ${OBSERVED_DATE} ($(wc -l < "$GOLD_DIR/totals_history.csv") total observations)."

# 5. VALIDATE THE BUILT ARTEFACTS
log_orchestrator "Validating generated assets..."
jq -e . "$WORK_DIR/stats.json" >/dev/null || die "Generated stats.json is not valid JSON."
CSV_ROWS=$(( $(wc -l < "$WORK_DIR/combined_licences.csv") - 1 ))
[ "$CSV_ROWS" -gt 0 ] || die "Generated CSV has no data rows."
[ -s "$WORK_DIR/combined_licences.duckdb" ] || die "Generated DuckDB file is empty."
[ -s "$WORK_DIR/licensee_analytics.csv" ] || die "Generated analytics CSV is empty."
[ "$(wc -l < "$WORK_DIR/sample_assignments.csv")" -gt 1 ] || die "Generated preview sample has no data rows."
DB_ROWS=$(duckdb "$WORK_DIR/combined_licences.duckdb" -noheader -list "SELECT COUNT(*) FROM licences;" | tr -d '[:space:]')
[ "$DB_ROWS" = "$TOTAL_LICENSES" ] || log_orchestrator "NOTE: DuckDB holds ${DB_ROWS} rows vs ${TOTAL_LICENSES} JSON records (multi-line field values)."

# 6. PUBLISH ATOMICALLY
log_orchestrator "Publishing assets to ${BRONZE_DIR} and ${SILVER_DIR}..."
mkdir -p "$BRONZE_DIR" "$SILVER_DIR"
install -m 0644 "$WORK_DIR/combined_licences.json" "$BRONZE_DIR/combined_licences.json"
for asset in combined_licences.json combined_licences.csv combined_licences.duckdb licensee_analytics.csv sample_assignments.csv stats.json; do
    install -m 0644 "$WORK_DIR/$asset" "$SILVER_DIR/$asset"
done

rm -f "$PAGES_DIR"/page_*.json

log_orchestrator "Data processing and analytics complete!"
log_orchestrator "Assets created in ${SILVER_DIR}"

summary "### RSM data refresh"
summary ""
summary "| Metric | Value |"
summary "| --- | --- |"
summary "| Pages fetched | ${TOTAL_PAGES} |"
summary "| Licences | ${TOTAL_LICENSES} (previous: ${PREVIOUS_TOTAL}) |"
summary "| Unique holders | ${UNIQUE_HOLDERS} |"
summary "| Duplicate licence IDs | ${DUPLICATE_IDS} |"
