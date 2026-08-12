# Real-time New Zealand Radio Spectrum Data Portal

![Live Data Portal](https://github.com/spectrumefficiencylimited/sel-current/blob/main/SpectrumEfficiencyLimited.png)

**Live Data Portal:** [**https://spectrumefficiencylimited.github.io/sel-current/**](https://spectrumefficiencylimited.github.io/sel-current/)

This repository provides a modern, automated, and publicly accessible data portal for New Zealand's radio spectrum licenses. The data is fetched automatically every hour from the official [NZ Government Radio Spectrum Management (RSM) API](https://www.rsm.govt.nz/developers/), processed, and made available for download in multiple formats.

---

## A Modern Successor to Spectrum Search Lite

The official "Spectrum Search Lite" tool, which provided a downloadable Microsoft Access database of the Register of Radio Frequencies (RRF), was a valuable resource for engineers and analysts. However, **this service was decommissioned, and its final data update was in December 2022.**

This project was created to fill that gap by serving as a modern, automated, and more accessible successor:

- **Providing Fresh Data:** Instead of a static database, this portal provides data that is refreshed **every hour**.
- **Using Open, Standard Formats:** We provide data in CSV, JSON, and DuckDB formats, which are easily consumed by modern programming languages and data analysis tools, removing the dependency on Microsoft Access.
- **Being Fully Automated:** The entire data pipeline runs on its own via GitHub Actions, ensuring the data is always as current as the API allows.
- **Offering Historical Insight:** The headline figures and the top-holder rankings are committed to this repository every hour, building a version-controlled time series of the NZ radio spectrum, something not previously possible.
- **Enhanced Analytics:** The portal includes pre-calculated summaries of top license holders and key statistics.

This tool aims to empower the next generation of spectrum analysis with reliable, timely, and easy-to-use data.

---

## Features

- **Automated Updates:** Data is automatically fetched and refreshed hourly using GitHub Actions
- **Live Data Portal:** An easy-to-use web interface to view key statistics and data samples
- **Multiple Data Formats:** Download the complete dataset as **CSV**, **JSON**, or a **DuckDB** database file
- **Data Analytics:** Pre-calculated summaries of top license holders by assignment count
- **Open and Accessible:** All code and data are publicly available, encouraging transparency and community use
- **Secure:** API keys are managed securely using GitHub Secrets and are not exposed in the repository
- **Historical Records:** An hourly, version-controlled history of the headline statistics and top licence holders

---

## How It Works

This project is powered entirely by GitHub Actions, running on an hourly schedule. Here is the complete automation process:

1. **Fetch:** A Bash script (`scripts/fetch-process-data.sh`) calls the RSM API to fetch all current license assignments. The script handles pagination and parallel requests for efficiency.
2. **Process:** The raw JSON data from the API is combined and processed using `jq` for JSON manipulation.
3. **Transform:** Data is converted into multiple formats and analyzed using DuckDB for advanced analytics.
4. **Store:** The combined raw data is written to `bronze/combined_licences.json` and processed into `silver/` as `.csv`, `.json` and `.duckdb`, alongside `stats.json`, `licensee_analytics.csv` and a small `sample_assignments.csv` used by the web page.
5. **Commit:** Only the small artefacts — `stats.json`, `licensee_analytics.csv` and `sample_assignments.csv`, about 2 KB a run — are committed to `main`, building a time series of the headline numbers. The multi-megabyte datasets are build outputs and are deliberately **not** committed; see "Where the data lives" below.
6. **Deploy:** `index.html` and the complete `silver/` datasets are published to the `gh-pages` branch as a fresh orphan commit, served as a live website by GitHub Pages.

This entire cycle requires zero manual intervention and ensures data is always current.

### Where the data lives

The full CSV, JSON and DuckDB downloads are always available, always current,
and always regenerated from scratch by the workflow — they are served from the
site, not from git history:

| Asset | Committed to `main` | Published to the site |
| --- | --- | --- |
| `silver/stats.json` | ✅ hourly (~130 B) | ✅ |
| `silver/licensee_analytics.csv` | ✅ hourly (~800 B) | ✅ |
| `silver/sample_assignments.csv` | ✅ hourly (~1 KB) | ✅ |
| `silver/combined_licences.csv` | ❌ (8.5 MB) | ✅ |
| `silver/combined_licences.json` | ❌ (34 MB) | ✅ |
| `silver/combined_licences.duckdb` | ❌ (2.6 MB) | ✅ |
| `bronze/combined_licences.json` | ❌ (34 MB, duplicate of the silver JSON) | ❌ |

Committing every snapshot previously added roughly **80 MB of git history per
hour**. Publishing them to the site instead keeps every download available while
holding repository growth to a couple of kilobytes a run.

> **Note on timing:** GitHub queues scheduled workflows on a shared pool, so a run
> triggered at the top of the hour typically starts 10–40 minutes late. The
> `lastUpdateUTC` field in `silver/stats.json` is always the authoritative
> timestamp for the published data.

---

## Pipeline Reliability

The refresh is unattended, so the pipeline is built to fail safe: **it either
publishes a complete, sane dataset or it changes nothing at all.**

- **Bounded, retried network calls.** Every API request has a connect and total
  timeout and is retried with exponential backoff (including a bounded retry on
  HTTP 429), so a slow or rate-limiting API can never hang the job.
- **Per-page validation.** A page is only accepted when it returns HTTP 200 and
  parses as JSON containing an `items` array. Error bodies and truncated
  responses are retried, never combined into the dataset.
- **Gap filling.** If a page still fails after the parallel pass, it is retried
  serially. Only a page that cannot be fetched at all aborts the run — one flaky
  request no longer kills a whole refresh.
- **Atomic publish.** All assets are built in a temporary directory and are
  copied over `bronze/` and `silver/` only after passing validation, so a failed
  run always leaves the last good snapshot in place.
- **Data quality gates.** A run aborts before publishing if the record count
  falls below `RSM_MIN_LICENCES` (default 1000) or drops more than
  `RSM_MAX_DROP_PCT` (default 10%) below the previously published count. Use the
  `allow_data_drop` input on a manual run when the register genuinely shrank.
- **Cached DuckDB CLI.** The CLI is restored from the Actions cache and, on a
  cache miss, downloaded with retries, falling back to the DuckDB PyPI wheel if
  GitHub's release CDN is unavailable. Transient CDN failures were historically
  the single biggest cause of failed runs.
- **Serialised, race-tolerant pushes.** A `concurrency` group prevents
  overlapping runs, and if the branch moves under a run anyway, the commit is
  rebuilt on the new tip rather than failing on a non-fast-forward push.
- **Bounded runtime.** The job is capped at 20 minutes (a healthy run takes ~2).

Tunable environment variables: `RSM_PAGE_SIZE`, `RSM_PARALLELISM`,
`RSM_MAX_ATTEMPTS`, `RSM_CONNECT_TIMEOUT`, `RSM_MAX_TIME`, `RSM_MAX_PAGES`,
`RSM_MIN_LICENCES`, `RSM_MAX_DROP_PCT`, `ALLOW_DATA_DROP`.

---

## How to Use the Data

You can access the data in several ways:

### 1. Through the Web Portal

Visit the [live data portal](https://spectrumefficiencylimited.github.io/sel-current/) to:
- View the latest statistics and data summaries
- See a sample of recent assignments and top license holders
- Download the complete datasets directly from your browser

### 2. Direct Download Links

Use `curl` or other tools to download the latest data directly:

- **CSV:**
  ```bash
  curl -L -o rsm_data.csv https://spectrumefficiencylimited.github.io/sel-current/silver/combined_licences.csv
  ```
- **JSON:**
  ```bash
  curl -L -o rsm_data.json https://spectrumefficiencylimited.github.io/sel-current/silver/combined_licences.json
  ```
- **DuckDB:**
  ```bash
  curl -L -o rsm_data.duckdb https://spectrumefficiencylimited.github.io/sel-current/silver/combined_licences.duckdb
  ```

### 3. Cloning the Repository

Cloning gets you the pipeline and the hourly time series of headline statistics
and top licence holders:

```bash
git clone https://github.com/spectrumefficiencylimited/sel-current.git
```

The full datasets are **not** in git history — download them from the links
above, which always serve the most recent run.

---

## Technical Overview

- **Data Source:** [Radio Spectrum Management (RSM) API](https://www.rsm.govt.nz/developers/)
- **Automation:** [GitHub Actions](https://github.com/features/actions) on an hourly schedule
- **Data Processing:** `Bash`, `jq` (for JSON manipulation), and `DuckDB` (for analytics and transformation)
- **Frontend:** Single-page application using HTML, CSS, and vanilla JavaScript
- **Hosting:** [GitHub Pages](https://pages.github.com/) with automated deployment
- **Data Architecture:**
  - `bronze/`: Raw combined JSON from the API (build output, not committed)
  - `silver/`: Cleaned, production-ready datasets (CSV, JSON, DuckDB) plus the small
    analytics and statistics files. Only the small files are committed; the full
    datasets are published to the site.
- **Security:** API credentials managed through GitHub Secrets

---

## Contributing

Contributions are welcome! If you have ideas for new analytics, improvements to the web interface, or bug fixes, please feel free to open an issue or submit a pull request. Areas where contributions would be particularly valuable include:

- Additional data visualizations
- Enhanced analytics and reporting features
- Performance optimizations
- Mobile interface improvements
- Documentation enhancements

## License

This project is open-source and available under the [MIT License](LICENSE). The data itself is sourced from the NZ Government and is subject to its own terms of use. By using this service, you agree to comply with the official RSM API terms and conditions.
