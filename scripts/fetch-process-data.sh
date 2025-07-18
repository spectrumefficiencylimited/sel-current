#!/bin/bash
# Fetches API pages, combines them, and processes into final data assets.
# ----------------------------------------------------------------------
set -euo pipefail

# --- CONFIGURATION ---
# API Key is read from the environment variable RSM_API_KEY
if [ -z "${RSM_API_KEY:-}" ]; then
    echo "FATAL: RSM_API_KEY environment variable is not set."
    exit 1
fi

BRONZE_DIR="./bronze"
PAGES_DIR="${BRONZE_DIR}/pages"
SILVER_DIR="./silver"

mkdir -p "$PAGES_DIR" "$SILVER_DIR"

# --- LOGGING FUNCTIONS ---
log_orchestrator() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') - ORCHESTRATOR: $1"
}

log_worker() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') - WORKER[$$]: $1"
}

# --- API FETCH FUNCTION (WORKER) ---
fetch_api_page() {
    local page_num=$1
    local output_path="$PAGES_DIR/page_${page_num}.json"
    local url="https://api.business.govt.nz/gateway/radio-spectrum-management/v1/licences?page=${page_num}&page-size=1000&sort-by=Licence%20ID&sort-order=desc&txRx=TRN&licenceStatus=CURRENT&gridRefDefault=LAT_LONG_NZGD2000_D2000"

    log_worker "Fetching page $page_num..."
    
    local attempt=0
    local max_retries=3
    
    while [ $attempt -lt $max_retries ]; do
        local tmp_file
        tmp_file=$(mktemp "$PAGES_DIR/page_${page_num}.tmp.XXXXXX")

        if http_code=$(curl --location -s -w "%{http_code}" -H "Ocp-Apim-Subscription-Key: $RSM_API_KEY" "$url" -o "$tmp_file"); then
            if [ "$http_code" -eq 200 ] && jq -e . "$tmp_file" >/dev/null 2>&1; then
                mv "$tmp_file" "$output_path"
                log_worker "SUCCESS: Page $page_num saved."
                return 0
            elif [ "$http_code" -eq 429 ]; then
                log_worker "RATE LIMITED: Sleeping 4s..."
                sleep 4
            else
                log_worker "WARN (Attempt $((attempt+1))/$max_retries): HTTP $http_code"
                sleep 5
                attempt=$((attempt + 1))
            fi
        else
            log_worker "CURL ERROR: Retrying in 5s..."
            sleep 5
            attempt=$((attempt + 1))
        fi
        rm -f "$tmp_file"
    done
    
    log_worker "FATAL: Failed to fetch page $page_num after $max_retries attempts."
    return 1
}

# --- SCRIPT ENTRY POINT ---
if [ "$#" -eq 1 ] && [ "$1" != "orchestrator" ]; then
    page_to_fetch=$1
    fetch_api_page "$page_to_fetch"
    exit $?
fi

# --- ORCHESTRATOR MODE ---
log_orchestrator "Starting data fetch and process..."
rm -rf "$PAGES_DIR"/*.json

# 1. FETCHING DATA
# ... (This part remains unchanged)
log_orchestrator "Fetching initial page to determine total pages..."
if ! curl --location -s -H "Ocp-Apim-Subscription-Key: $RSM_API_KEY" \
    "https://api.business.govt.nz/gateway/radio-spectrum-management/v1/licences?page=1&page-size=1000&sort-by=Licence%20ID&sort-order=desc&txRx=TRN&licenceStatus=CURRENT&gridRefDefault=LAT_LONG_NZGD2000_D2000" \
    > "$PAGES_DIR/page_1.json"; then
    log_orchestrator "FATAL: Could not fetch initial page. Exiting."
    exit 1
fi

TOTAL_PAGES=$(jq -r '.totalPages // 1' "$PAGES_DIR/page_1.json")
if ! [[ "$TOTAL_PAGES" =~ ^[0-9]+$ ]] || [ "$TOTAL_PAGES" -eq 0 ]; then
    log_orchestrator "FATAL: Invalid total pages found: '$TOTAL_PAGES'. Exiting."
    exit 1
fi
log_orchestrator "Total pages to fetch: $TOTAL_PAGES"

if [ "$TOTAL_PAGES" -gt 1 ]; then
    log_orchestrator "Spawning parallel workers for pages 2 to $TOTAL_PAGES..."
    seq 2 "$TOTAL_PAGES" | xargs -P 8 -I{} bash "$0" {}
fi

# 2. COMBINING DATA (BRONZE LAYER)
log_orchestrator "Combining all page JSONs into a single file..."
jq -s '[.[].items] | add' "$PAGES_DIR"/page_*.json > "$BRONZE_DIR/combined_licences.json"

# 3. PROCESSING DATA (SILVER LAYER)
log_orchestrator "Processing data into Silver layer assets..."

cp "$BRONZE_DIR/combined_licences.json" "$SILVER_DIR/combined_licences.json"

# Create final CSV
log_orchestrator "Generating CSV file..."
echo "licenceId,licenceNumber,licensee,channel,frequency,location,status,txrx,suppressed" > "$SILVER_DIR/combined_licences.csv"
jq -r '.[] | [.licenceID, .licenceNumber, .licensee, .channel, .frequency, .location, .status, .txrx, .suppressed] | @csv' \
  "$BRONZE_DIR/combined_licences.json" >> "$SILVER_DIR/combined_licences.csv"

# Create DuckDB file
log_orchestrator "Generating DuckDB file..."
duckdb "$SILVER_DIR/combined_licences.duckdb" "CREATE OR REPLACE TABLE licences AS SELECT * FROM read_csv_auto('$SILVER_DIR/combined_licences.csv', ALL_VARCHAR=TRUE);"

# Create statistics file for the website
log_orchestrator "Generating statistics file..."
TOTAL_LICENSES=$(jq 'length' "$BRONZE_DIR/combined_licences.json")
UNIQUE_HOLDERS=$(jq '[.[] | .licensee] | unique | length' "$BRONZE_DIR/combined_licences.json")

cat > "$SILVER_DIR/stats.json" << EOF
{
  "totalLicenses": ${TOTAL_LICENSES},
  "activeAssignments": ${TOTAL_LICENSES},
  "uniqueHolders": ${UNIQUE_HOLDERS},
  "lastUpdateUTC": "$(date -u --iso-8601=seconds)"
}
EOF

# 4. PERFORM AND EXPORT ANALYTICS (NEW SECTION)
log_orchestrator "Generating licensee analytics..."
duckdb "$SILVER_DIR/combined_licences.duckdb" <<EOF
COPY (
    SELECT
        licensee,
        COUNT(*) AS assignment_count
    FROM licences
    WHERE location != 'MOBILE'
    GROUP BY licensee
    ORDER BY assignment_count DESC
    LIMIT 25
) TO '$SILVER_DIR/licensee_analytics.csv' (HEADER, DELIMITER ',');
EOF

log_orchestrator "Data processing and analytics complete!"
log_orchestrator "Assets created in $SILVER_DIR"
