import hashlib
import re
from pathlib import Path

import psycopg2
from psycopg2.extras import RealDictCursor


DB_CONFIG = {
    "dbname": "hiring_scout",
    "user": "",      # Set your PostgreSQL user (default is often your Mac username or 'postgres')
    "password": "",  # Set your password if configured, or leave empty for peer/local auth
    "host": "localhost",
    "port": 5432,
}


def get_connection():
    return psycopg2.connect(**{key: value for key, value in DB_CONFIG.items() if value != ""})


def compute_fingerprint(company: str, role: str) -> str:
    clean_str = f"{company.strip().lower()}:::{role.strip().lower()}"
    return hashlib.md5(clean_str.encode("utf-8")).hexdigest()


def parse_score(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def split_markdown_table_row(line: str) -> list[str] | None:
    stripped = line.strip()
    if not (stripped.startswith("|") and stripped.endswith("|")):
        return None
    return [cell.strip().replace(r"\|", "|") for cell in re.split(r"(?<!\\)\|", stripped[1:-1])]


def compact_table_rows(section: str) -> tuple[list[str], list[list[str]]]:
    table_lines = [line for line in section.splitlines() if line.strip().startswith("|")]
    if len(table_lines) < 3:
        return [], []

    headers = split_markdown_table_row(table_lines[0])
    separator = split_markdown_table_row(table_lines[1])
    if not headers or not separator or len(headers) != len(separator):
        return [], []
    if not all(re.fullmatch(r":?-{3,}:?", cell.replace(" ", "")) for cell in separator):
        return [], []

    rows: list[list[str]] = []
    for line in table_lines[2:]:
        cells = split_markdown_table_row(line)
        if cells and len(cells) == len(headers):
            rows.append(cells)
    return headers, rows


def compact_table_jobs(section: str, bucket: str) -> list[dict]:
    headers, rows = compact_table_rows(section)
    expected_headers = {
        "weak_fit": ["Score", "Company", "Title", "Location", "Salary", "HN URL", "Main concern"],
        "hard_filtered": ["Company", "Title", "Location", "Salary", "HN URL", "Reasons"],
        "low_priority": [
            "Company",
            "Title",
            "Heuristic",
            "Location",
            "Salary",
            "Positive signals",
            "Negative signals",
        ],
    }
    if headers != expected_headers.get(bucket):
        return []

    jobs = []
    for cells in rows:
        values = dict(zip(headers, cells))
        company = values["Company"]
        role_title = values["Title"]
        if not company or not role_title:
            continue

        if bucket == "weak_fit":
            summary = None
            concerns = values["Main concern"]
            hard_filter_reasons = None
            score = parse_score(values["Score"])
        elif bucket == "hard_filtered":
            summary = "Skipped by deterministic hard filters before LLM matching."
            concerns = values["Reasons"]
            hard_filter_reasons = values["Reasons"]
            score = None
        else:
            summary = f"Positive signals: {values['Positive signals']}"
            concerns = values["Negative signals"]
            hard_filter_reasons = None
            score = parse_score(values["Heuristic"])

        jobs.append(
            {
                "company": company,
                "role_title": role_title,
                "score": score,
                "bucket": bucket,
                "location": values["Location"],
                "salary": values["Salary"],
                "tech_stack": None,
                "hn_url": values.get("HN URL"),
                "apply_url": None,
                "summary": summary,
                "why_it_fits": None,
                "concerns": concerns,
                "evidence_quotes": None,
                "hard_filter_reasons": hard_filter_reasons,
            }
        )
    return jobs


def parse_report_markdown(file_path: str):
    content = Path(file_path).read_text(encoding="utf-8")

    # 1. Extract report metadata.
    report_title_match = re.search(r"^#\s+(.+)$", content, re.MULTILINE)
    report_title = report_title_match.group(1).strip() if report_title_match else Path(file_path).stem

    date_match = re.search(r"Generated:\s*([^\n\r]+)", content)
    report_date = date_match.group(1).strip() if date_match else None

    thread_match = re.search(r"URL:\s*(https?://[^\s\n]+)", content)
    thread_url = thread_match.group(1).strip() if thread_match else None

    comments_match = re.search(r"Total comments:\s*(\d+)", content)
    total_comments = int(comments_match.group(1)) if comments_match else None

    # 2. Extract detailed job blocks and compact-table rows.
    sections = re.split(r"\n##\s+", content)
    jobs = []

    bucket_map = {
        "must apply": "must_apply",
        "strong maybe": "strong_maybe",
        "maybe": "maybe",
        "weak fit": "weak_fit",
        "hard filtered / skipped": "hard_filtered",
        "not llm-ranked / low heuristic priority": "low_priority",
    }

    for section in sections[1:]:
        sec_header = section.split("\n", 1)[0].strip().lower()
        bucket = bucket_map.get(sec_header, "other")
        if bucket in {"weak_fit", "hard_filtered", "low_priority"}:
            jobs.extend(compact_table_jobs(section, bucket))

        job_blocks = re.split(r"\n###\s+", section)
        for block in job_blocks[1:]:
            lines = block.strip().split("\n")
            title_line = lines[0].strip()
            if not title_line or title_line.lower() == "draft outreach":
                continue

            # Format is typically "Company - Role Title".
            if " - " in title_line:
                company, role_title = title_line.split(" - ", 1)
            else:
                company, role_title = title_line, "Unknown Role"

            company = company.strip()
            role_title = role_title.strip()
            body = "\n".join(lines[1:])

            score_match = re.search(r"-\s*Score:\s*([0-9\.]+)", body)
            score = parse_score(score_match.group(1) if score_match else None)

            loc_match = re.search(r"-\s*Location/remote:\s*([^\n]+)", body)
            loc = loc_match.group(1).strip() if loc_match else None

            salary_match = re.search(r"-\s*Salary:\s*([^\n]+)", body)
            salary = salary_match.group(1).strip() if salary_match else None

            stack_match = re.search(r"-\s*Tech stack:\s*([^\n]+)", body)
            stack = stack_match.group(1).strip() if stack_match else None

            hn_match = re.search(r"-\s*HN comment:\s*(https?://[^\s\n]+)", body)
            hn_url = hn_match.group(1).strip() if hn_match else None

            apply_match = re.search(r"-\s*Apply:\s*([^\n]+)", body)
            apply_url = apply_match.group(1).strip() if apply_match else None

            def extract_subfield(name):
                pattern = rf"{name}:\s*\n(.*?)(?=\n[A-Z][a-zA-Z\s]+:|\n###|\Z)"
                match = re.search(pattern, body, re.DOTALL)
                return match.group(1).strip() if match else None

            jobs.append(
                {
                    "company": company,
                    "role_title": role_title,
                    "score": score,
                    "bucket": bucket,
                    "location": loc,
                    "salary": salary,
                    "tech_stack": stack,
                    "hn_url": hn_url,
                    "apply_url": apply_url,
                    "summary": extract_subfield("Summary"),
                    "why_it_fits": extract_subfield("Why it fits"),
                    "concerns": extract_subfield("Concerns"),
                    "evidence_quotes": extract_subfield("Evidence quotes"),
                    "hard_filter_reasons": extract_subfield("Hard filter reasons"),
                }
            )

    return {
        "title": report_title,
        "date": report_date,
        "source_file": Path(file_path).name,
        "thread_url": thread_url,
        "total_comments": total_comments,
        "jobs": jobs,
    }


def ingest_file(file_path: str):
    parsed = parse_report_markdown(file_path)
    conn = get_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)

    try:
        # Register report.
        cur.execute(
            """
            INSERT INTO reports (report_title, report_date, source_file, thread_url, total_comments)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (source_file) DO UPDATE SET total_comments = EXCLUDED.total_comments
            RETURNING id;
            """,
            (
                parsed["title"],
                parsed["date"],
                parsed["source_file"],
                parsed["thread_url"],
                parsed["total_comments"],
            ),
        )
        report_id = cur.fetchone()["id"]

        for job in parsed["jobs"]:
            clean_company = job["company"].lower().strip()
            cur.execute(
                """
                INSERT INTO companies (name, clean_name)
                VALUES (%s, %s)
                ON CONFLICT (name) DO UPDATE SET clean_name = EXCLUDED.clean_name
                RETURNING id, is_banned;
                """,
                (job["company"], clean_company),
            )
            company_id = cur.fetchone()["id"]
            fingerprint = compute_fingerprint(clean_company, job["role_title"])

            cur.execute(
                """
                INSERT INTO jobs (
                    company_id, role_title, fingerprint, score, bucket, location_remote,
                    salary, tech_stack, hn_comment_url, apply_url, summary, why_it_fits,
                    concerns, evidence_quotes, hard_filter_reasons, first_seen_report_id, last_seen_report_id
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                )
                ON CONFLICT (fingerprint) DO UPDATE SET
                    company_id = EXCLUDED.company_id,
                    role_title = EXCLUDED.role_title,
                    score = EXCLUDED.score,
                    bucket = EXCLUDED.bucket,
                    location_remote = COALESCE(EXCLUDED.location_remote, jobs.location_remote),
                    salary = COALESCE(EXCLUDED.salary, jobs.salary),
                    tech_stack = COALESCE(EXCLUDED.tech_stack, jobs.tech_stack),
                    hn_comment_url = COALESCE(EXCLUDED.hn_comment_url, jobs.hn_comment_url),
                    apply_url = COALESCE(EXCLUDED.apply_url, jobs.apply_url),
                    summary = COALESCE(EXCLUDED.summary, jobs.summary),
                    why_it_fits = COALESCE(EXCLUDED.why_it_fits, jobs.why_it_fits),
                    concerns = COALESCE(EXCLUDED.concerns, jobs.concerns),
                    evidence_quotes = COALESCE(EXCLUDED.evidence_quotes, jobs.evidence_quotes),
                    hard_filter_reasons = COALESCE(
                        EXCLUDED.hard_filter_reasons,
                        jobs.hard_filter_reasons
                    ),
                    last_seen_report_id = EXCLUDED.last_seen_report_id,
                    repost_count = jobs.repost_count + 1,
                    updated_at = NOW();
                """,
                (
                    company_id,
                    job["role_title"],
                    fingerprint,
                    job["score"],
                    job["bucket"],
                    job["location"],
                    job["salary"],
                    job["tech_stack"],
                    job["hn_url"],
                    job["apply_url"],
                    job["summary"],
                    job["why_it_fits"],
                    job["concerns"],
                    job["evidence_quotes"],
                    job["hard_filter_reasons"],
                    report_id,
                    report_id,
                ),
            )

        conn.commit()
        print(f"Successfully ingested {len(parsed['jobs'])} jobs from {file_path}")
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()
        conn.close()


if __name__ == "__main__":
    import sys

    report_file = sys.argv[1] if len(sys.argv) > 1 else "hn_whos_hiring_2026-10_report.md"
    ingest_file(report_file)
