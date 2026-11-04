# Investigation: Dashboard duplicate Streamlit element key

## Summary

The failure is caused by a key-scope mismatch, not by Streamlit rerunning the script incorrectly. `render_job_review()` renders one card per `jobs` row, but the hide-company widgets use only `company_id` in their keys. A company can have multiple job rows, so the same company-level key is instantiated more than once; the reported `hide_reason_13` is the concrete example.

## Symptoms

- `hiring-dashboard/app.py:565-568` creates a `st.text_input` with `key=f"hide_reason_{item['company_id']}"`.
- Streamlit reports duplicate key `hide_reason_13` while rendering `render_job_review()`.
- The log also reports a separate deprecation warning for `st.components.v1.html`; this is not the cause of the duplicate-key exception.

## Investigator Findings

### Render flow and key collision

- `hiring-dashboard/app.py:383-395` selects from `jobs j JOIN companies c`, returning job columns plus `c.id AS company_id`; there is no `DISTINCT`, grouping, or one-row-per-company constraint.
- `hiring-dashboard/app.py:424-425` executes the query, and `hiring-dashboard/app.py:452` iterates every returned row as `item`.
- The per-job controls correctly use `item['id']` in their keys (`app.py:490-541`), establishing that job IDs are the row/card identity.
- The company hide popover instead uses company-only keys at `app.py:558`, `565-570`: `card_unhide_{company_id}`, `hide_reason_{company_id}`, and `hide_{company_id}`.
- Therefore, if two jobs share company ID 13 and are both visible/unhidden, both cards create `hide_reason_13` and Streamlit raises `StreamlitDuplicateElementKey`.

### Data model and deduplication

- `hiring-dashboard/schema/tables.sql:5-12` makes `companies.id` the company identity and makes company `name` unique.
- `hiring-dashboard/schema/tables.sql:27-32` gives `jobs` its own primary key and a unique `fingerprint`; `company_id` is only a foreign key/index (`:29`, `:64`), not unique.
- This intentionally permits multiple jobs/roles for one company. Job deduplication is by `fingerprint`, not by `company_id` (`tables.sql:31`).
- `hiring-dashboard/injest.py:277-291` resolves/reuses a company ID and upserts jobs by fingerprint, so multiple distinct roles for one company naturally retain the same `company_id`.
- The SQL join is consequently expected to produce multiple rows for a company with multiple jobs; it is not evidence of accidental duplicate company records.

### Secondary warning

- `hiring-dashboard/app.py:8` imports `streamlit.components.v1`, and `app.py:72-75` calls `components.html(...)` for the copyable email widget. This matches the deprecation warning in the log and should be migrated separately, but it does not generate `hide_reason_*` keys.

### Trace of the rendered item list

- `hiring-dashboard/app.py:300-381` defines `render_job_review()` and gathers the sidebar filters. These filters constrain the query but do not de-duplicate by company.
- The review list is built at `hiring-dashboard/app.py:383-425`: the SQL selects one `jobs` row (`j.id` at `:385`) and labels the joined company primary key `c.id AS company_id` (`:390`), joins `jobs j` to `companies c` and `reports r` (`:392-394`), applies optional filters (`:398-422`), and assigns the fetched rows to `results` (`:424-425`). There is no `DISTINCT`, `GROUP BY`, or company-level reduction.
- `hiring-dashboard/app.py:452` iterates `for item in results`, so every returned job row renders a card. `hidden_companies` (`:302-317`) is only the sidebar list, and `reports` (`:325-342`) only supplies report-filter options; neither is the card item list.

### Exact collision and data cause

- `hiring-dashboard/app.py:565-569` creates `st.text_input(..., key=f"hide_reason_{item['company_id']}")`. Since the key uses the company ID rather than the selected job ID, two cards for company 13 both instantiate `hide_reason_13`. The sibling company-scoped buttons have the same collision pattern at `:558` and `:570`.
- `hiring-dashboard/schema/tables.sql:27-31` makes `jobs.id` the row primary key and `jobs.fingerprint` unique, while `jobs.company_id` (`:29`) is only a foreign key. Multiple jobs per company are therefore valid database rows.
- `hiring-dashboard/injest.py:23-25` computes each fingerprint from normalized company **and role**. Ingestion resolves the company at `:266-278`, inserts the job at `:282-289`, and uses `ON CONFLICT (fingerprint)` at `:290-311`; only the same company/role fingerprint is merged. Distinct roles at one company remain separate job rows sharing one `company_id`.
- Live database evidence: `SELECT company_id, count(*) FROM jobs GROUP BY company_id HAVING count(*) > 1` returned IDs 11, 13, 19, 22, 26, 32, 36, 37, 56, and 62. For the reported ID, company 13 (`Innolitics`) has `jobs.id=13` (`Staff Medical Device Software Engineer`) and `jobs.id=15` (`Principal Medical Device Software Engineer`), with different fingerprints; both satisfy the review query and render.

**Conclusion:** the query is job-granular but the widget key is company-granular. Duplicate `company_id` values are therefore expected in `results`; the precise failure is the repeated key construction at `app.py:565-569`, enabled by the non-unique `jobs.company_id` schema and role-inclusive fingerprint/upsert behavior.

## Investigation Log

### Initial triage - 2026-11-04
**Hypothesis:** duplicate company IDs are rendered because job review is a job-level list while hide widgets are company-keyed.
**Findings:** confirmed in the query, loop, schema, and ingest upsert path. The reported key maps exactly to company ID 13.
**Evidence:** `hiring-dashboard/app.py:383-425`, `:452`, `:558-575`; `hiring-dashboard/schema/tables.sql:27-32`; `hiring-dashboard/injest.py:277-311`.
**Conclusion:** confirmed.

### Alternate hypothesis: rerun/session-state artifact
**Hypothesis:** the same widget is being created twice only because Streamlit reruns or stale session state retain old elements.
**Findings:** not supported. Streamlit registers elements for the current script run; the current run itself creates the same explicit user key once per matching job row. Reruns expose the deterministic collision but are not its source.
**Conclusion:** eliminated as primary cause.

### Alternate hypothesis: duplicate company rows in the database
**Hypothesis:** a bad company join creates duplicate company records.
**Findings:** company names are unique, and the relevant one-to-many relationship is intentional: `jobs.company_id` is non-unique while each job has its own primary key/fingerprint.
**Conclusion:** eliminated as the underlying defect; repeated company IDs across job rows are expected.

## Root Cause

The widget keys do not match the rendering identity. `render_job_review()` renders a separate card for each job (`results` row), but `hide_reason`, `hide`, and `card_unhide` use only `company_id`. Multiple roles for the same company therefore create duplicate Streamlit widget keys. Company 13 has at least two rows in the result set in the failing run, producing the reported `hide_reason_13` collision.

## Recommendations

1. **Primary fix:** make every widget key unique per rendered card by including `item['id']`, e.g. use `hide_reason_{item['id']}`, `hide_{item['id']}`, and `card_unhide_{item['id']}` at `hiring-dashboard/app.py:558-570`. Keep the SQL update parameter as `item['company_id']`, because the action still changes company visibility. This is the smallest change and preserves the current job-card layout.
2. **Alternative UX fix:** if hiding is intended to appear only once per company, group/aggregate the result cards by company and render one company-level hide control. This is a larger behavior change and should not be used merely as a key workaround.
3. **Optional defensive check:** add a diagnostic assertion/log after `run_query()` that reports duplicate `company_id` values, but do not deduplicate the result set because distinct jobs/roles must remain visible.
4. **Separate maintenance:** migrate `render_copyable_email()` away from deprecated `components.html` at `hiring-dashboard/app.py:72-75`, following the Streamlit API supported by the installed version. Do this independently of the duplicate-key fix.

## Preventive Measures

- Define key prefixes around the entity being rendered: use `job_id` for job-card widgets and `company_id` only for widgets rendered once per company.
- When adding a widget inside a loop, review whether the chosen identifier is unique over the loop, not merely unique in its source table.
- Add a small test/helper that checks generated widget keys for uniqueness over representative results containing multiple jobs for one company.
- After fixes, test a dataset with at least two distinct roles sharing one company and verify both cards render and each hide action updates the intended company.
