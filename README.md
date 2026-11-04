# HN Hiring Scout

HN Hiring Scout is a local Python tool for scanning Hacker News "Who is Hiring?" threads. It fetches top-level HN job posts, prefilters obvious mismatches, uses an OpenAI-compatible LLM to extract and rank relevant roles against your profile, generates a Markdown report, and can sync shortlisted jobs into a Google Sheets CRM. An optional local Streamlit dashboard can ingest those reports into PostgreSQL for interactive review and company moderation.

It is intentionally simple:

- JSON files for raw and processed data
- Markdown for reports
- Google Sheets for lightweight CRM tracking
- optional local PostgreSQL + Streamlit dashboard

## Index

- [Overview](#hn-hiring-scout)
- [Quick start: monthly workflow](#quick-start-monthly-workflow)
  - [Install dependencies](#1-install-dependencies)
  - [Configure the environment](#2-configure-the-environment)
  - [Create or edit a profile](#3-create-or-edit-a-profile)
  - [Fetch an HN thread](#4-fetch-an-hn-thread)
  - [Analyze jobs](#5-analyze-jobs)
  - [Recover from an LLM provider failure](#recover-from-an-llm-provider-failure)
  - [Sync jobs to Google Sheets](#6-sync-jobs-to-google-sheets)
- [Optional PostgreSQL and Streamlit dashboard](#optional-postgresql-and-streamlit-dashboard)
  - [Initialize the database](#2-initialize-the-database-and-tables)
  - [Ingest a report](#3-parse-and-ingest-a-report)
  - [Launch the dashboard](#4-launch-the-web-dashboard)
  - [Future reports and deduplication](#future-reports-deduplication-and-hidden-companies)
- [Project layout](#project-layout)
- [Detailed command reference](#detailed-command-reference)
  - [`generate_profile.py`](#generate_profilepy)
  - [`fetch_hn.py`](#fetch_hnpy)
  - [`analyze_jobs.py`](#analyze_jobspy)
  - [`sync_google_sheet.py`](#sync_google_sheetpy)
- [Google Sheets CRM notes](#google-sheets-crm-notes)
- [Common recipes](#common-recipes)
- [Safety notes](#safety-notes)

## Quick start: monthly workflow

Use this flow when you want to analyze the full thread without missing eligible jobs, while still avoiding LLM calls for obvious mismatches.

### 1. Install dependencies

A virtual environment is recommended:

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install --upgrade pip
python3 -m pip install -r requirements.txt
```

The requirements file includes the PostgreSQL driver and Streamlit dependencies used by the optional dashboard.

### 2. Configure the environment

Copy the environment template:

```bash
cp env.example .env
```

Then edit `.env` with your LLM provider details:

```bash
LLM_API_KEY=your_api_key
LLM_BASE_URL=https://api.fireworks.ai/inference/v1
LLM_MODEL=accounts/fireworks/models/llama-v3p1-70b-instruct
```

For OpenAI directly, leave `LLM_BASE_URL` empty.

If you want Google Sheets sync, also set:

```bash
GOOGLE_SHEET_ID=your_google_sheet_id
GOOGLE_SHEETS_CREDENTIALS_FILE=/absolute/path/to/service-account.json
```

Share the Google Sheet with the service account email from the credentials JSON.

### 3. Create or edit a profile

If you want to generate a draft profile from your resume, case studies, notes, or an entire career-profile folder:

```bash
python3 generate_profile.py \
  --input "/path/to/resume.pdf" \
  --input "/path/to/case-studies" \
  --output profile.generated.yaml \
  --ask-questions
```

Preview what files will be read without calling the LLM:

```bash
python3 generate_profile.py --input ~/Career_Profile --dry-run
```

The optional questions are skippable and help fill fields that resumes often omit, such as LinkedIn, GitHub, website, email, location, timezone, work authorization, and compensation. The generator sends extracted document text to your configured LLM provider. Review the generated geography, work authorization, compensation, and hard filters before using the profile for job analysis.

You can also edit a profile manually. Use one of:


```bash
profile.yaml
profile.generated.yaml
```

The profile controls:

- target roles
- avoid roles
- skills
- domain preferences
- remote and geography rules
- compensation minimums
- hard filters
- LLM scoring weights
- outreach email style

Be especially careful with geography and work authorization fields. The prefilter uses them to reject region-locked roles such as Remote US, EU only, Japan only, or onsite-only jobs.

### 4. Fetch an HN thread

Use the HN item ID:

```bash
python3 fetch_hn.py --item-id 48747975 --month 2026-07
```

Or use the HN URL:

```bash
python3 fetch_hn.py --url "https://news.ycombinator.com/item?id=48747975" --month 2026-07
```

This writes:

```text
data/raw/hn_whos_hiring_2026-07_raw.json
```

Only top-level comments are fetched. Nested replies are ignored.

### 5. Analyze jobs

This is the recommended command when you are fine with token usage but still want to avoid LLM calls for clear mismatches (change XXXXX with your profile yaml name): 

```bash
python3 analyze_jobs.py \
  --month 2026-07 \
  --profile profile.XXXXX.yaml \
  --report-mode full \
  --generate-outreach \
  --yes
```

This does all of the following:

1. loads the raw HN comments
2. runs deterministic comment-level prefiltering
3. skips obvious junior, onsite-only, wrong-geo, unpaid, and irrelevant posts before extraction
4. extracts structured job data using the LLM
5. applies role-level hard filters
6. computes heuristic scores
7. LLM-ranks every eligible role
8. optionally generates outreach drafts for strong matches
9. writes JSON caches and a Markdown report

Outputs:

```text
data/processed/hn_whos_hiring_2026-07_prefiltered.json
data/processed/hn_whos_hiring_2026-07_extracted.json
data/processed/hn_whos_hiring_2026-07_matches.json
data/reports/hn_whos_hiring_2026-07_report.md
```

Do not pass `--limit`, `--max-extractions`, or `--top-n-for-matching` if you want all eligible jobs analyzed.

### Recover from an LLM provider failure

The analyzer now stops immediately if an extraction or matching request fails. Its error
message includes the operation, comment/job ID, model, base URL, HTTP status, and provider
request ID when available. A `404` means the configured `LLM_MODEL` is not available at the
configured `LLM_BASE_URL` for the current API key; correct the provider configuration before
retrying.

To discard only the generated artifacts for one month and start its analysis again, while
preserving the fetched raw HN input, use:

```bash
python3 analyze_jobs.py \
  --month 2026-09 \
  --profile profile.gaurav.yaml \
  --clear-analysis \
  --yes
```

`--clear-analysis` removes that month's prefilter, extraction cache, match cache, and report.
It never deletes `data/raw/`.

### 6. Sync jobs to Google Sheets

Dry-run first:

```bash
python3 sync_google_sheet.py --month 2026-07 --dry-run
```

Then sync:

```bash
python3 sync_google_sheet.py --month 2026-07
```

By default, the sync imports only:

- `must_apply`
- `strong_maybe`
- `maybe`

The Google Sheet has these tabs:

- `Jobs`: compact CRM view for applications and follow-ups
- `Details`: longer AI summaries, reasons, concerns, and outreach drafts
- `Followups`: generated list of jobs needing follow-up
- `README`: sheet usage notes

If you redesigned or regenerated the current month and want to repopulate that month's rows:

```bash
python3 sync_google_sheet.py --month 2026-07 --replace-month
```

Matching openings keep manual CRM fields such as application status, dates, notes, and follow-up data.

## Optional PostgreSQL and Streamlit dashboard

The Streamlit dashboard imports generated Markdown reports into local PostgreSQL and provides filtering, application tracking, company hiding, raw Apply details, and the original HN post. The dashboard source is already in this repository at `hiring-dashboard/`; do not create an empty directory elsewhere.

### 1. Create and activate a dashboard virtual environment

From the repository root:

```bash
cd hiring-dashboard
python3 -m venv venv
source venv/bin/activate
pip install psycopg2-binary streamlit pandas
cd ..
```

### 2. Initialize the database and tables

Ensure PostgreSQL 14 is running (for example, if it is configured to start at login). For a fresh setup, run the schema from the repository root:

```bash
psql -h localhost -d postgres -f hiring-dashboard/schema/tables.sql
```

`tables.sql` creates and connects to the `hiring_scout` database, so do not run a separate `CREATE DATABASE` command. It is only for first-time setup. To upgrade an existing database, run these migrations in order instead:

```bash
psql -h localhost -d hiring_scout -f hiring-dashboard/schema/001_personal_job_workflow.sql
psql -h localhost -d hiring_scout -f hiring-dashboard/schema/002_application_reporting.sql
```

Set the same `DB_CONFIG` values in `hiring-dashboard/injest.py` and `hiring-dashboard/app.py` for your PostgreSQL user, password, host, and port. The default database name is `hiring_scout`.

### 3. Parse and ingest a report

Place the generated report in `hiring-dashboard/`, then run the ingester. (The script is named `injest.py` in this repository.)

```bash
cd hiring-dashboard
python injest.py hn_whos_hiring_2026-10_report.md
```

### 4. Launch the web dashboard

With the dashboard virtual environment still active, run:

```bash
streamlit run app.py
```

Streamlit prints a local URL, normally `http://localhost:8501`; open it in a browser if it does not open automatically. Use **Job review** to filter openings, apply/save/skip a job, and hide companies. Confirming an application records its date and application method; skipping requires a reason. Use **Application report** in the sidebar to see applied, skipped, and yet-to-apply jobs, the first monthly report in which each job appeared, and editable comments next to each company.

### Future reports, deduplication, and hidden companies

| Requirement | Implementation |
| --- | --- |
| Ingest a new report | From `hiring-dashboard/`, run `python injest.py <new_report_file.md>`. The ingester creates a report record and links its incoming jobs. |
| Detect reposts | The pipeline computes an MD5 fingerprint from `clean_company + ":::" + clean_role`. When a company reposts the same role in a later month, PostgreSQL increments `repost_count`, updates the score and last-seen report, and avoids a duplicate job row. |
| Hide a company | Select **Hide company** in the dashboard. This sets `companies.is_hidden` to `TRUE`, immediately excluding that company’s current and future roles from active dashboard views. |
| Store new report fields | The `jobs.metadata` JSONB column is available for arbitrary new key-value pairs from future scout-report formats without a schema migration. |

## Project layout

### Scripts

`fetch_hn.py`

Fetches the HN story and its top-level comments using the official Hacker News Firebase API.

`analyze_jobs.py`

Runs prefiltering, LLM extraction, hard filtering, heuristic scoring, LLM matching, outreach generation, and Markdown report generation.

`generate_profile.py`

Generates a validated profile YAML from resumes, case studies, notes, PDFs, text files, DOCX files, or directories of candidate material.

`sync_google_sheet.py`

Syncs processed job results into a Google Sheets CRM.

`hiring-dashboard/injest.py`

Imports a generated Markdown report into the local PostgreSQL dashboard database.

`hiring-dashboard/app.py`

Runs the Streamlit dashboard for filtering jobs, tracking application status, and managing hidden companies.

`hiring-dashboard/schema/tables.sql`

Creates the local PostgreSQL database tables and indexes used by the dashboard.

### Configuration

`profile.yaml`

Editable generic profile template.

`profile.gaurav.yaml`

Personalized profile file.

`env.example`

Environment variable template. Copy to `.env`.

`.env`

Local secrets and provider settings. Do not commit this file.

`requirements.txt`

Python dependencies.

### Data Directories

`data/raw/`

Raw HN thread comments.

`data/processed/`

Prefilter results, LLM extraction cache, and match cache.

`data/reports/`

Generated Markdown reports.

`credentials/`

Optional local location for Google service account credentials. Do not commit private credentials.

## Detailed command reference

### `generate_profile.py`

Generates a validated HN Hiring Scout profile YAML from candidate documents.

#### Basic usage

```bash
python3 generate_profile.py \
  --input "/path/to/resume.pdf" \
  --input "/path/to/case-studies" \
  --output profile.generated.yaml
```

You can pass files, directories, or both. Directories are scanned recursively.

```bash
python3 generate_profile.py \
  --input "/path/to/resume.pdf" \
  --input "/path/to/case-study.pdf" \
  --input "/path/to/notes" \
  --output profile.generated.yaml
```

#### Dry run

```bash
python3 generate_profile.py --input ~/Career_Profile --dry-run
```

Dry-run extracts local text and prints file sizes. It does not call the LLM and does not write a profile.

#### Supported input files

- PDF files through `pypdf`
- DOCX files through `python-docx`
- text-like files such as `.txt`, `.md`, `.yaml`, `.yml`, `.json`, `.csv`, and `.tsv`
- unknown extensions when they safely decode as text

Hidden files are skipped. Very large files are skipped by default to avoid accidentally sending irrelevant binary data.

#### Options

`--input`

File or directory to use as evidence. Can be repeated.

`--output`

Required unless using `--dry-run` or `--self-test`. Generated YAML path.

`--template`

Profile schema/template source. Defaults to `profile.yaml`.

```bash
python3 generate_profile.py --input ~/Career_Profile --output profile.generated.yaml --template profile.yaml
```

`--max-chars`

Maximum evidence characters used for the final profile generation call. If extracted text is larger, the script summarizes chunks with the LLM first.

`--force`

Overwrite the output file if it already exists.

`--include-private-links`

Allows the LLM to populate personal links found in the documents. By default, links stay as placeholders or null unless you provide them through optional questions or a context file.

`--include-evidence-section`

Adds an `evidence_profile` section with short source-backed notes. This is enabled by default because it makes the generated profile more grounded and useful.

`--no-evidence-section`

Disables the optional `evidence_profile` section.

`--ask-questions`

Asks skippable questions for fields that documents often miss, such as LinkedIn, GitHub, website, email, location, timezone, work authorization, and compensation. Press Enter to skip a field, or type `skip all`.

```bash
python3 generate_profile.py --input ~/Career_Profile --output profile.generated.yaml --ask-questions
```

`--no-questions`

Skips optional questions even when running in an interactive terminal.

`--context-file`

Loads optional user-supplied context from a JSON or YAML file. This is useful when you want repeatable profile generation without answering questions every time.

Example context file:

```yaml
name: "Your Name"
email: "you@example.com"
website: "https://example.com"
linkedin: "https://www.linkedin.com/in/example/"
github: "https://github.com/example"
country: "India"
region: "Gujarat"
city: "Ahmedabad"
timezone: "Asia/Kolkata"
can_work_in_us: false
can_work_in_eu: false
can_work_in_uk: false
requires_visa_sponsorship: true
work_authorization: "authorized to work in India; open to remote contractor roles globally"
```

`--self-test`

Runs local validation and extraction tests.

```bash
python3 generate_profile.py --self-test
```

#### Schema safety

`generate_profile.py` treats `profile.yaml` as the live schema. It validates that generated profiles keep the same top-level sections and nested shapes, preserve geography/work authorization fields, include `evidence_profile` by default, and keep `scoring_weights` summed to 100. If `profile.yaml` changes and the generated result no longer matches, the run fails before writing the profile.

Document contents are sent to your configured LLM provider. Review generated geography, work authorization, compensation, and hard filters before using the profile for analysis.

### `fetch_hn.py`

Fetches one HN thread and saves top-level comments to `data/raw/`.

#### Basic usage

```bash
python3 fetch_hn.py --item-id 48747975 --month 2026-07
```

```bash
python3 fetch_hn.py --url "https://news.ycombinator.com/item?id=48747975" --month 2026-07
```

#### Options

`--item-id`

HN story item ID. Use either `--item-id` or `--url`.

`--url`

HN story URL. The script extracts the `id` from the URL.

`--month`

Required. Used in the output filename. Format: `YYYY-MM`.

`--delay`

Delay between comment fetches, in seconds. Default is `0.05`.

Use a larger delay if you want to be extra gentle with the API:

```bash
python3 fetch_hn.py --item-id 48747975 --month 2026-07 --delay 0.2
```

`--output-dir`

Where raw JSON should be written. Default is `data/raw`.

```bash
python3 fetch_hn.py --item-id 48747975 --month 2026-07 --output-dir data/raw
```

### `analyze_jobs.py`

Analyzes raw HN comments and writes processed JSON plus a Markdown report.

#### Recommended full analysis with prefilter

```bash
python3 analyze_jobs.py \
  --month 2026-07 \
  --profile profile.gaurav.yaml \
  --report-mode full \
  --generate-outreach \
  --yes
```

This still runs prefiltering. It does not spend LLM calls on clear mismatches.

#### Quick test run without LLM

```bash
python3 analyze_jobs.py \
  --month 2026-07 \
  --profile profile.gaurav.yaml \
  --dry-run
```

Dry-run uses heuristics instead of real LLM extraction and ranking. Use it to check plumbing, not final decisions.

#### Prefilter only

```bash
python3 analyze_jobs.py \
  --month 2026-07 \
  --profile profile.gaurav.yaml \
  --prefilter-only
```

This writes the prefilter JSON and exits before any LLM calls.

#### Budgeted run

```bash
python3 analyze_jobs.py \
  --month 2026-07 \
  --profile profile.gaurav.yaml \
  --max-extractions 100 \
  --top-n-for-matching 40
```

Use this when you want to control token usage.

#### Fresh rerun

```bash
python3 analyze_jobs.py \
  --month 2026-07 \
  --profile profile.gaurav.yaml \
  --force-extract \
  --force-match \
  --generate-outreach \
  --yes
```

Use this after changing prompts, filters, profile preferences, or scoring logic.

#### Options

`--month`

Required unless using `--self-test`. Chooses the input and output filenames.

`--profile`

Required unless using `--self-test`. Path to the YAML profile.

`--raw-file`

Use a specific raw JSON file instead of the default monthly file.

```bash
python3 analyze_jobs.py --month 2026-07 --profile profile.gaurav.yaml --raw-file data/raw/hn_whos_hiring_2026-07_raw.json
```

`--limit`

Limits raw comments before prefiltering. This is mainly for testing.

```bash
python3 analyze_jobs.py --month 2026-07 --profile profile.gaurav.yaml --limit 20
```

`--max-extractions`

Limits how many prefilter-surviving comments are sent to the LLM for extraction.

`--top-n-for-matching`

Limits how many eligible roles are sent to the LLM for matching after hard filters and heuristic sorting.

`--force-extract`

Ignores extraction cache and re-extracts comments.

`--force-match`

Ignores match cache and re-ranks jobs.

`--clear-analysis`

Deletes the selected month's generated prefilter, extraction, match, and report files before running again. It preserves the raw HN input in `data/raw/`.

`--generate-outreach`

Generates outreach email drafts for `must_apply` jobs or jobs with score `>= 85`.

`--report-mode shortlist`

Default. Produces a readable report with full detail for higher-priority jobs and compact sections for lower-priority jobs.

```bash
python3 analyze_jobs.py --month 2026-07 --profile profile.gaurav.yaml --report-mode shortlist
```

`--report-mode full`

Produces a more detailed report for review and debugging.

`--include-deprioritized`

Includes comments that the prefilter marked as deprioritized. Useful if you think the prefilter is being too strict.

`--prefilter-only`

Runs only the comment-level prefilter and exits before LLM calls.

`--dry-run`

Runs without LLM calls or LLM environment variables. Produces heuristic fake extraction and matching results.

`--show-heuristic-details`

Prints the top heuristic-ranked jobs and their positive/negative signals before matching.

`--yes`

Skips confirmation if the run will make more than 50 new LLM calls.

`--self-test`

Runs local tests for JSON parsing, filters, salary parsing, geo matching, scoring helpers, and related logic.

```bash
python3 analyze_jobs.py --self-test
```

### `sync_google_sheet.py`

Syncs analyzed jobs into Google Sheets.

#### Basic usage

```bash
python3 sync_google_sheet.py --month 2026-07
```

This uses `GOOGLE_SHEET_ID` and `GOOGLE_SHEETS_CREDENTIALS_FILE` from `.env`.

You can also pass them explicitly:

```bash
python3 sync_google_sheet.py \
  --month 2026-07 \
  --spreadsheet-id your_google_sheet_id \
  --credentials-file /absolute/path/to/service-account.json
```

#### Dry-run

```bash
python3 sync_google_sheet.py --month 2026-07 --dry-run
```

Computes what would be imported without writing to Google Sheets.

#### Repopulate one month

```bash
python3 sync_google_sheet.py --month 2026-07 --replace-month
```

This removes stale rows associated with the current month and re-adds the latest report rows. Existing matching openings keep manual CRM fields.

#### Include lower-priority jobs

Include weak fits:

```bash
python3 sync_google_sheet.py --month 2026-07 --include-weak-fit
```

Include skips, hard-filtered jobs, and not-LLM-ranked jobs:

```bash
python3 sync_google_sheet.py --month 2026-07 --include-skips
```

#### Options

`--month`

Required unless using `--self-test`. Selects the month to sync.

`--spreadsheet-id`

Google Sheet ID. Optional if `GOOGLE_SHEET_ID` is set in `.env`.

`--credentials-file`

Google service account JSON file. Optional if `GOOGLE_SHEETS_CREDENTIALS_FILE` is set in `.env`.

`--raw-file`

Override the raw HN JSON file.

`--extracted-file`

Override the extracted jobs JSON file.

`--matches-file`

Override the matches JSON file.

`--dry-run`

Shows planned changes without writing to Google Sheets.

`--replace-month`

Repopulates rows for the selected month from the latest processed JSON.

`--include-weak-fit`

Also syncs jobs with bucket `weak_fit`.

`--include-skips`

Also syncs skipped, hard-filtered, and not-LLM-ranked jobs.

`--self-test`

Runs local tests for key generation and merge behavior.

```bash
python3 sync_google_sheet.py --self-test
```

## Google Sheets CRM Notes

The sync creates or updates these tabs:

`Jobs`

Main CRM view. Company is first, followed by title, score, heuristic score, location, remote status, salary, tech stack, application tracking fields, source links, and housekeeping fields.

`Details`

Longer AI-generated fields, including summary, positive reasons, concerns, missing information, hard-filter reasons, and outreach drafts.

`Followups`

Generated view of rows where follow-up is needed or due.

`README`

Explains the sheet layout and which columns are auto-managed.

Manual CRM columns are preserved on sync:

- `application_status`
- `application_date`
- `application_channel`
- `last_contacted_on`
- `response_status`
- `response_date`
- `followup_needed`
- `followup_date`
- `next_action`
- `contact_name`
- `contact_email`
- `notes`
- `do_not_apply`

## Common Recipes

### Generate a profile from all career documents

```bash
python3 generate_profile.py --input ~/Career_Profile --output profile.generated.yaml --ask-questions
```

Then review `profile.generated.yaml` before using it:

```bash
python3 analyze_jobs.py --month 2026-07 --profile profile.generated.yaml --dry-run
```

### Generate a profile with source-backed notes

```bash
python3 generate_profile.py \
  --input ~/Career_Profile \
  --output profile.generated.yaml \
  --include-evidence-section
```

### Analyze everything eligible, but still skip obvious mismatches

```bash
python3 analyze_jobs.py --month 2026-07 --profile profile.gaurav.yaml --report-mode full --generate-outreach --yes
```

### Spend fewer tokens

```bash
python3 analyze_jobs.py --month 2026-07 --profile profile.gaurav.yaml --max-extractions 80 --top-n-for-matching 30
```

### Debug prefilter decisions

```bash
python3 analyze_jobs.py --month 2026-07 --profile profile.gaurav.yaml --prefilter-only
```

Then inspect:

```text
data/processed/hn_whos_hiring_2026-07_prefiltered.json
```

### Debug heuristic ranking

```bash
python3 analyze_jobs.py --month 2026-07 --profile profile.gaurav.yaml --show-heuristic-details --top-n-for-matching 20
```

### Re-run after changing the profile

```bash
python3 analyze_jobs.py --month 2026-07 --profile profile.gaurav.yaml --force-match --generate-outreach --yes
```

### Re-run after changing extraction prompts

```bash
python3 analyze_jobs.py --month 2026-07 --profile profile.gaurav.yaml --force-extract --force-match --generate-outreach --yes
```

### Sync only good opportunities to CRM

```bash
python3 sync_google_sheet.py --month 2026-07
```

### Sync a broader review list

```bash
python3 sync_google_sheet.py --month 2026-07 --include-weak-fit
```

## Safety Notes

- `.env` contains secrets. Do not commit it.
- Google service account JSON files contain secrets. Do not commit them.
- The LLM is instructed not to hallucinate, but you should still verify important facts before applying.
- Hard filters are conservative. Ambiguous cases should usually go to LLM ranking instead of being rejected early.
- Cached JSON results are reused when comment/job hashes match. Use `--force-extract` or `--force-match` when you intentionally want fresh LLM results.
