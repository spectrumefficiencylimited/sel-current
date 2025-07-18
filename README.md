# Real-time New Zealand Radio Spectrum Management (RSM) Data

![RSM Data Portal](https://user-images.githubusercontent.com/your-github-id/your-image-id.png) <!-- Optional: Add a screenshot of your live page here -->

**Live Data Portal:** [**https://spectrumefficiencylimited.github.io/sel-current/**](https://spectrumefficiencylimited.github.io/sel-current/)

This repository contains the infrastructure and data for a publicly accessible portal that provides real-time access to New Zealand's radio spectrum license data. The data is fetched automatically from the official [NZ Government Radio Spectrum Management (RSM) API](https://www.rsm.govt.nz/developers/), processed, and made available for download in multiple formats.

The primary goal of this project is to make valuable public data more accessible, transparent, and useful for researchers, hobbyists, and industry professionals.

---

## Features

- **Automated Updates:** The data is automatically fetched and refreshed every 30 minutes using GitHub Actions.
- **Live Data Portal:** An easy-to-use web interface to view key statistics and data samples.
- **Multiple Data Formats:** Download the complete dataset as **CSV**, **JSON**, or a **DuckDB** database file.
- **Data Analytics:** The portal includes a pre-calculated summary of the top license holders by assignment count.
- **Open and Accessible:** All code and data are publicly available, encouraging transparency and community use.
- **Secure:** API keys are managed securely using GitHub Secrets and are not exposed in the repository.

---

## How It Works

This project is powered entirely by GitHub Actions, running on a 30-minute schedule. Here is a high-level overview of the automation process:

1.  **Fetch:** A Bash script (`scripts/fetch-process-data.sh`) calls the RSM API to fetch all current license assignments. The script handles pagination and parallel requests for efficiency.
2.  **Process:** The raw JSON data from the API is combined and processed.
3.  **Store:**
    *   The combined raw data is stored as `bronze/combined_licences.json`.
    *   The processed data is saved into multiple formats (`.csv`, `.json`, `.duckdb`) in the `silver/` directory.
    *   Key statistics and analytics are generated and saved to `silver/stats.json` and `silver/licensee_analytics.csv`.
4.  **Commit:** The updated data files are committed back to the `main` branch of this repository, creating a historical record of the data over time.
5.  **Deploy:** The `index.html` file and the `silver/` data assets are pushed to a dedicated `gh-pages` branch, which is automatically published as a live website using GitHub Pages.

This entire cycle requires zero manual intervention.

---

## How to Use the Data

You can access the data in several ways:

### 1. Through the Web Portal

Visit the [live data portal](https://spectrumefficiencylimited.github.io/sel-current/) to:
- View the latest statistics.
- See a sample of recent assignments.
- Download the complete datasets directly from your browser.

### 2. Direct Download Links

You can use `curl` or other tools to download the latest data directly.

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

You can clone this repository to get the full history of the data updates.

```bash
git clone https://github.com/spectrumefficiencylimited/sel-current.git
```

---

## Technology Stack

- **Data Source:** [Radio Spectrum Management (RSM) API](https://www.rsm.govt.nz/developers/)
- **Automation:** [GitHub Actions](https://github.com/features/actions)
- **Data Processing:** `Bash`, `jq` (for JSON manipulation), and `DuckDB` (for analytics)
- **Frontend:** HTML, CSS, and vanilla JavaScript
- **Hosting:** [GitHub Pages](https://pages.github.com/)

---

## Contributing

While the core process is fully automated, contributions are welcome! If you have ideas for new analytics, improvements to the web interface, or bug fixes, please feel free to open an issue or submit a pull request.

## License

This project is open-source and available under the [MIT License](LICENSE). The data itself is sourced from the NZ Government and is subject to its own terms of use.
