# LinkedInfoSec README

> **Based on the original project by [@ahessmat](https://github.com/ahessmat)**
> Original repository: [https://github.com/ahessmat/LinkedInfoSec](https://github.com/ahessmat/LinkedInfoSec)
>
> This fork extends the original CLI scraper with macOS compatibility fixes,
> Chrome browser support, a Flask web UI with persistent job history, CSV downloads,
> cancel/edit/delete, and enriched per-job output (description, open date, close date).

The LinkedInfoSec project was born from the desire to identify exactly which certifications prospective employers are looking for right now. The tool datascrapes LinkedIn's public-facing job search endpoint for job listings that contain certifications, then outputs .CSV formatted files with all of the information it grabs.

## Project Status

This project is currently paused for later continuation.

Current state:
- Core scraping + parsing + Flask UI are working.
- Resume/cover-letter generation is integrated in the web app.
- CSV download outputs include richer job metadata for review/export workflows.

When resuming later, start by pulling latest `main`, re-running a quick scrape,
and validating output formats before adding new features.

## Quick Start (macOS/Linux)

1. Create and activate a virtual environment:

```bash
python3 -m venv .env
source .env/bin/activate
```

2. Install dependencies:

```bash
pip install -r requirements.txt
```

3. Ensure Firefox is installed (required by Selenium).

4. Run a quick scrape:

```bash
python scrape.py -j "cybersecurity" -l "remote" -q -o cyber
```

5. Parse extracted cert counts:

```bash
python handle.py -f cyber_allinfo.csv
```

The python script requires the following non-standard libraries and their dependencies:

1. selenium
2. requests
3. pandas
4. tqdm

The following flags may be passed to scrape.py to tailor your results:

* **-j** or **--job**: The job title or keyword to search for. The default is "cybersecurity". Example: **python3 scrape.py -j "penetration tester"**
* **-t** or **--time**: How recent the listings to be scraped should be. Example: **python3 scrape.py -t day**
* **-s** or **--seniority**: The leves of seniority (1-5, from Intern to Director-level) to process as input. Each level should be explicitly named for inclusion. Example: **python3 scrape.py -s 12345**
* **-l** or **--locations**: The geographic area to consider jobs. Default is 'remote'. Example: **python3 scrape.py -l "London"**
* **-i** or **--increment**: The increment of time (in seconds) that should be allowed to let jobs load for scraping. Example **python3 scrape.py -i 3**
* **-o** or **--output**: The name of the file to output scrape results to. Example **python3 scrape.py -o sec**
* **-q** or **--quick**: Only parse the first 50 listings. Example **python3 scrape.py -q**
* **-max**: The maximum number of jobs that should be processed. Example **python3 scrape.py --max 500**
* **-b** or **--browser**: Browser to run with (`firefox` or `chrome`). Default is `firefox`. Example **python3 scrape.py -b chrome**
* **--keep-open**: Keep browser open after the script finishes. Useful for debugging page state. Example **python3 scrape.py -b chrome --keep-open**

Regardless of whether you specify a name for the outfile with (-o), the script writes multiple CSV artifacts:

- `<output>_allinfo.csv` columns: `job_id`, `title`, `certs_set`, `description`, `open_date`, `close_date`
- `<output>_data.csv` columns: `job_id`, `title`, `cert`, `job_url`, `open_date`, `close_date`, `certs`, `description`
- `<output>_all_certs.csv` / `<output>_certs.csv`: aggregated cert counts

You can then output a sorted list of certifications by count by running **handle.py -f <file_allinfo.csv>**.

## Web App (Automated UI)

You can run the project as a local web app where `scrape.py` and `handle.py` run in the background.

1. Install dependencies:

```bash
pip install -r requirements.txt
```

2. Start the web app:

```bash
python app.py
```

3. Open your browser at `http://127.0.0.1:5000`.

4. Fill the form and submit. The app will:
	- run `scrape.py` in the background,
	- run `handle.py` on the generated output,
	- show status, logs, and certification counts on a results page.

Web app features:

* Persistent job history across restarts (SQLite database: `jobs.db`)
* Cancel running jobs from the job details page
* Download generated CSV artifacts (`allinfo`, `data`, `all_certs`) from the UI

## Troubleshooting

* If `python handle.py -f cyber_allinfo.csv` prints `{}`, your scrape likely did not capture certifications in that run.
* LinkedIn frequently changes page markup, and some jobs simply do not list cert requirements.
* `Job detail pane failed to render ...` is now informational: the scraper still uses HTTP page parsing fallback.
* Try a broader search and process more jobs:

```bash
python scrape.py -j "cybersecurity" -l "sydney" --max 200 -o cyber -b chrome -i 1.5
python handle.py -f cyber_allinfo.csv
```

* Keep the browser open for debugging page behavior:

```bash
python scrape.py -j "cybersecurity" -l "sydney" -q -o cyber -b chrome --keep-open
```
