# Real-time New Zealand Radio Spectrum Data Portal

![Live Data Portal](https://raw.githubusercontent.com/spectrumefficiencylimited/sel-current/main/docs/portal-screenshot.png)

**Live Data Portal:** [**https://spectrumefficiencylimited.github.io/sel-current/**](https://spectrumefficiencylimited.github.io/sel-current/)

This repository provides a modern, automated, and publicly accessible data portal for New Zealand's radio spectrum licenses. The data is fetched automatically every 30 minutes from the official [NZ Government Radio Spectrum Management (RSM) API](https://www.rsm.govt.nz/developers/), processed, and made available for download in multiple formats.

---

## A Modern Successor to Spectrum Search Lite

The official "Spectrum Search Lite" tool, which provided a downloadable Microsoft Access database of the Register of Radio Frequencies (RRF), was a valuable resource for engineers and analysts. However, **this service was decommissioned, and its final data update was in December 2022.**

This project was created to fill that gap by serving as a modern, automated, and more accessible successor:

- **Providing Fresh Data:** Instead of a static database, this portal provides data that is refreshed **every 30 minutes**.
- **Using Open, Standard Formats:** We provide data in CSV, JSON, and DuckDB formats, which are easily consumed by modern programming languages and data analysis tools, removing the dependency on Microsoft Access.
- **Being Fully Automated:** The entire data pipeline runs on its own via GitHub Actions, ensuring the data is always as current as the API allows.
- **Offering Historical Insight:** By committing the data back to this Git repository, we are building a version-controlled history of the NZ radio spectrum, something not previously possible.
- **Enhanced Analytics:** The portal includes pre-calculated summaries of top license holders and key statistics.

This tool aims to empower the next generation of spectrum analysis with reliable, timely, and easy-to-use data.

---

## Features

- **Automated Updates:** Data is automatically fetched and refreshed every 30 minutes using GitHub Actions
- **Live Data Portal:** An easy-to-use web interface to view key statistics and data samples
- **Multiple Data Formats:** Download the complete dataset as **CSV**, **JSON**, or a **DuckDB** database file
- **Data Analytics:** Pre-calculated summaries of top license holders by assignment count
- **Open and Accessible:** All code and data are publicly available, encouraging transparency and community use
- **Secure:** API keys are managed securely using GitHub Secrets and are not exposed in the repository
- **Historical Records:** Complete version history of spectrum data changes over time

---

## How It Works

This project is powered entirely by GitHub Actions, running on a 30-minute schedule. Here is the complete automation process:

1. **Fetch:** A Bash script (`scripts/fetch-process-data.sh`) calls the RSM API to fetch all current license assignments. The script handles pagination and parallel requests for efficiency.
2. **Process:** The raw JSON data from the API is combined and processed using `jq` for JSON manipulation.
3. **Transform:** Data is converted into multiple formats and analyzed using DuckDB for advanced analytics.
4. **Store:**
   - The combined raw data is stored as `bronze/combined_licences.json`
   - The processed data is saved into multiple formats (`.csv`, `.json`, `.duckdb`) in the `silver/` directory
   - Key statistics and analytics are generated and saved to `silver/stats.json` and `silver/licensee_analytics.csv`
5. **Commit:** The updated data files are committed back to the `main` branch of this repository, creating a historical record of the data over time.
6. **Deploy:** The `index.html` file and the `silver/` data assets are pushed to a dedicated `gh-pages` branch, which is automatically published as a live website using GitHub Pages.

This entire cycle requires zero manual intervention and ensures data is always current.

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

You can clone this repository to get the full history of the data updates:

```bash
git clone https://github.com/spectrumefficiencylimited/sel-current.git
```

The primary data files are located in the `/silver` directory.

---

## Technical Overview

- **Data Source:** [Radio Spectrum Management (RSM) API](https://www.rsm.govt.nz/developers/)
- **Automation:** [GitHub Actions](https://github.com/features/actions) with 30-minute scheduling
- **Data Processing:** `Bash`, `jq` (for JSON manipulation), and `DuckDB` (for analytics and transformation)
- **Frontend:** Single-page application using HTML, CSS, and vanilla JavaScript
- **Hosting:** [GitHub Pages](https://pages.github.com/) with automated deployment
- **Data Architecture:**
  - `bronze/`: Stores the raw, combined JSON data from the API
  - `silver/`: Contains the cleaned, production-ready datasets (CSV, JSON, DuckDB) and analytics files
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
