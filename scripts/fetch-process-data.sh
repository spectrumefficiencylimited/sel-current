# --- ORCHESTRATOR MODE ---
log_orchestrator "Starting data fetch and process..."

# Clean up previous run completely
rm -rf "$PAGES_DIR"/*.json
rm -f "$SILVER_DIR"/*.csv "$SILVER_DIR"/*.json "$SILVER_DIR"/*.duckdb

# 1. FETCHING DATA
log_orchestrator "Fetching initial page to determine total pages..."
if ! curl --location -s -H "Ocp-Apim-Subscription-Key: $RSM_API_KEY" \
    "https://api.business.govt.nz/gateway/radio-spectrum-management/v1/licences?page=1&page-size=1000&sort-by=Licence%20ID&sort-order=desc&txRx=TRN&licenceStatus=CURRENT&gridRefDefault=LAT_LONG_NZGD2000_D2000" \
    > "$PAGES_DIR/page_1.json"; then
    log_orchestrator "FATAL: Could not fetch initial page. Exiting."; exit 1
fi
TOTAL_PAGES=$(jq -r '.totalPages // 1' "$PAGES_DIR/page_1.json")
if ! [[ "$TOTAL_PAGES" =~ ^[0-9]+$ ]] || [ "$TOTAL_PAGES" -eq 0 ]; then
    log_orchestrator "FATAL: Invalid total pages found: '$TOTAL_PAGES'. Exiting."; exit 1
fi
log_orchestrator "Total pages to fetch: $TOTAL_PAGES"
if [ "$TOTAL_PAGES" -gt 1 ]; then
    log_orchestrator "Spawning parallel workers for pages 2 to $TOTAL_PAGES..."; seq 2 "$TOTAL_PAGES" | xargs -P 8 -I{} bash "$0" {};
fi

# 2. COMBINING DATA
log_orchestrator "Combining all page JSONs into a single file..."
jq -s '[.[].items] | add' "$PAGES_DIR"/page_*.json > "$BRONZE_DIR/combined_licences.json"

# 3. PROCESSING DATA
log_orchestrator "Processing data into Silver layer assets..."
# Force overwrite existing files
cp -f "$BRONZE_DIR/combined_licences.json" "$SILVER_DIR/combined_licences.json"

# Overwrite CSV file completely
echo "licenceId,licenceNumber,licensee,channel,frequency,location,status,txrx,suppressed" > "$SILVER_DIR/combined_licences.csv"
jq -r '.[] | [.licenceID, .licenceNumber, .licensee, .channel, .frequency, .location, .status, .txrx, .suppressed] | @csv' \
  "$BRONZE_DIR/combined_licences.json" >> "$SILVER_DIR/combined_licences.csv"

# Remove existing DuckDB file and create fresh
rm -f "$SILVER_DIR/combined_licences.duckdb"
duckdb "$SILVER_DIR/combined_licences.duckdb" "CREATE OR REPLACE TABLE licences AS SELECT * FROM read_csv_auto('$SILVER_DIR/combined_licences.csv', ALL_VARCHAR=TRUE);"

# 4. PERFORM AND EXPORT ANALYTICS
log_orchestrator "Generating licensee and location analytics..."
# Remove existing analytics files first
rm -f "$SILVER_DIR/licensee_analytics.csv" "$SILVER_DIR/location_analytics.csv"

duckdb "$SILVER_DIR/combined_licences.duckdb" <<EOF
COPY (
    SELECT licensee, COUNT(*) AS assignment_count
    FROM licences WHERE location != 'MOBILE'
    GROUP BY licensee ORDER BY assignment_count DESC LIMIT 25
) TO '$SILVER_DIR/licensee_analytics.csv' (HEADER, DELIMITER ',');
EOF

duckdb "$SILVER_DIR/combined_licences.duckdb" <<EOF
COPY (
    SELECT location, COUNT(*) AS assignment_count
    FROM licences WHERE location != 'MOBILE' AND location IS NOT NULL
    GROUP BY location ORDER BY assignment_count DESC LIMIT 25
) TO '$SILVER_DIR/location_analytics.csv' (HEADER, DELIMITER ',');
EOF

# 5. GENERATE FINAL STATISTICS
log_orchestrator "Generating final statistics file..."
STATS_QUERY="
CREATE TEMP TABLE single_license_holders AS SELECT licensee FROM licences GROUP BY licensee HAVING COUNT(*) = 1;
SELECT
    (SELECT COUNT(*) FROM licences) AS totalAssignments,
    (SELECT COUNT(DISTINCT location) FROM licences WHERE location != 'MOBILE') AS totalLocations,
    (SELECT COUNT(DISTINCT licensee) FROM licences) AS totalLicensees,
    (SELECT COUNT(*) FROM single_license_holders) AS individualLicensees,
    (SELECT COUNT(*) FROM licences WHERE suppressed = 'true') AS suppressedCount;
"
# ** THE DEFINITIVE FIX IS HERE: Added -noheader flag **
STATS_RESULT=$(duckdb -noheader "$SILVER_DIR/combined_licences.duckdb" "$STATS_QUERY")

# Parse the pipe-separated result and trim whitespace
TOTAL_ASSIGNMENTS=$(echo "$STATS_RESULT" | cut -d'|' -f1 | xargs)
TOTAL_LOCATIONS=$(echo "$STATS_RESULT" | cut -d'|' -f2 | xargs)
TOTAL_LICENSEES=$(echo "$STATS_RESULT" | cut -d'|' -f3 | xargs)
INDIVIDUAL_LICENSEES=$(echo "$STATS_RESULT" | cut -d'|' -f4 | xargs)
SUPPRESSED_COUNT=$(echo "$STATS_RESULT" | cut -d'|' -f5 | xargs)

# Validate that all values are numeric, set to 0 if not
[[ ! "$TOTAL_ASSIGNMENTS" =~ ^[0-9]+$ ]] && TOTAL_ASSIGNMENTS=0
[[ ! "$TOTAL_LOCATIONS" =~ ^[0-9]+$ ]] && TOTAL_LOCATIONS=0
[[ ! "$TOTAL_LICENSEES" =~ ^[0-9]+$ ]] && TOTAL_LICENSEES=0
[[ ! "$INDIVIDUAL_LICENSEES" =~ ^[0-9]+$ ]] && INDIVIDUAL_LICENSEES=0
[[ ! "$SUPPRESSED_COUNT" =~ ^[0-9]+$ ]] && SUPPRESSED_COUNT=0

# Debug output to help troubleshoot
log_orchestrator "Stats values: $TOTAL_ASSIGNMENTS|$TOTAL_LOCATIONS|$TOTAL_LICENSEES|$INDIVIDUAL_LICENSEES|$SUPPRESSED_COUNT"

# Remove existing stats.json and create fresh
rm -f "$SILVER_DIR/stats.json"
cat > "$SILVER_DIR/stats.json" << EOF
{
  "totalAssignments": $TOTAL_ASSIGNMENTS,
  "totalLocations": $TOTAL_LOCATIONS,
  "totalLicensees": $TOTAL_LICENSEES,
  "individualLicensees": $INDIVIDUAL_LICENSEES,
  "suppressedCount": $SUPPRESSED_COUNT,
  "lastUpdateUTC": "$(date -u --iso-8601=seconds)"
}
EOF

# Validate the generated JSON
if ! jq -e . "$SILVER_DIR/stats.json" >/dev/null 2>&1; then
    log_orchestrator "ERROR: Generated stats.json is not valid JSON. Creating fallback."
    cat > "$SILVER_DIR/stats.json" << EOF
{
  "totalAssignments": 0,
  "totalLocations": 0,
  "totalLicensees": 0,
  "individualLicensees": 0,
  "suppressedCount": 0,
  "lastUpdateUTC": "$(date -u --iso-8601=seconds)"
}
EOF
fi

log_orchestrator "Data processing and analytics complete!"
log_orchestrator "Assets created in $SILVER_DIR"
