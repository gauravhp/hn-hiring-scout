#!/usr/bin/env python3
"""Sync HN Hiring Scout results into a Google Sheets CRM."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from collections import defaultdict
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - python-dotenv is in requirements.
    load_dotenv = None  # type: ignore[assignment]


JsonDict = dict[str, Any]

ROOT_DIR = Path(__file__).resolve().parent
DATA_DIR = ROOT_DIR / "data"
RAW_DIR = DATA_DIR / "raw"
PROCESSED_DIR = DATA_DIR / "processed"

JOBS_SHEET_NAME = "Jobs"
DETAILS_SHEET_NAME = "Details"
FOLLOWUPS_SHEET_NAME = "Followups"
README_SHEET_NAME = "README"

AUTO_COLUMNS = [
    "company",
    "title",
    "bucket",
    "score",
    "heuristic_score",
    "location",
    "remote_status",
    "salary",
    "tech_stack",
    "apply_url",
    "apply_email",
    "hn_url",
    "match_status",
    "summary",
    "positive_reasons",
    "concerns",
    "missing_info",
    "hard_filter_reasons",
    "draft_outreach_subject",
    "draft_outreach_body",
    "first_seen_month",
    "last_seen_month",
    "seen_months",
    "current_month_seen",
    "company_seen_before",
    "new_opening_for_existing_company",
    "opening_key",
    "company_key",
    "job_id",
    "comment_id",
    "last_synced_at",
]

MANUAL_COLUMNS = [
    "application_status",
    "application_date",
    "application_channel",
    "contact_name",
    "contact_email",
    "last_contacted_on",
    "response_status",
    "response_date",
    "followup_needed",
    "followup_date",
    "next_action",
    "notes",
    "do_not_apply",
]

JOBS_HEADER = [
    "company",
    "title",
    "bucket",
    "score",
    "heuristic_score",
    "location",
    "remote_status",
    "salary",
    "tech_stack",
    "application_status",
    "application_date",
    "application_channel",
    "last_contacted_on",
    "response_status",
    "response_date",
    "followup_needed",
    "followup_date",
    "next_action",
    "contact_name",
    "contact_email",
    "notes",
    "do_not_apply",
    "apply_url",
    "apply_email",
    "hn_url",
    "first_seen_month",
    "last_seen_month",
    "seen_months",
    "current_month_seen",
    "company_seen_before",
    "new_opening_for_existing_company",
    "opening_key",
    "company_key",
    "job_id",
    "comment_id",
    "last_synced_at",
]

DETAILS_COLUMNS = [
    "company",
    "title",
    "bucket",
    "score",
    "heuristic_score",
    "summary",
    "positive_reasons",
    "concerns",
    "missing_info",
    "hard_filter_reasons",
    "draft_outreach_subject",
    "draft_outreach_body",
    "match_status",
    "apply_url",
    "apply_email",
    "hn_url",
    "opening_key",
]

INTERNAL_COLUMNS = [
    "opening_key",
    "company_key",
    "job_id",
    "comment_id",
    "last_synced_at",
]

HOUSEKEEPING_COLUMNS = [
    "first_seen_month",
    "last_seen_month",
    "seen_months",
    "current_month_seen",
    "company_seen_before",
    "new_opening_for_existing_company",
]

FOLLOWUP_COLUMNS = [
    "company",
    "title",
    "followup_date",
    "next_action",
    "application_status",
    "response_status",
    "followup_needed",
    "contact_name",
    "contact_email",
    "application_date",
    "last_contacted_on",
    "apply_url",
    "hn_url",
    "notes",
]

DEFAULT_BUCKETS = {"must_apply", "strong_maybe", "maybe"}
VALID_MONTH_RE = re.compile(r"^\d{4}-\d{2}$")
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]


class SyncError(Exception):
    """Raised when the sync cannot continue."""


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def as_string(value: Any, default: str = "") -> str:
    if value is None:
        return default
    if isinstance(value, str):
        text = value.strip()
        return text if text else default
    return str(value).strip()


def as_string_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [as_string(item) for item in value if as_string(item)]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def as_number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def join_lines(items: Any) -> str:
    return "\n".join(as_string_list(items))


def join_csv(items: Any) -> str:
    return ", ".join(as_string_list(items))


def read_json_file(path: Path) -> JsonDict:
    if not path.exists():
        raise SyncError(f"Required file not found: {path}")
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except json.JSONDecodeError as exc:
        raise SyncError(f"Invalid JSON in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise SyncError(f"Expected JSON object in {path}")
    return data


def default_raw_path(month: str) -> Path:
    return RAW_DIR / f"hn_whos_hiring_{month}_raw.json"


def default_extracted_path(month: str) -> Path:
    return PROCESSED_DIR / f"hn_whos_hiring_{month}_extracted.json"


def default_matches_path(month: str) -> Path:
    return PROCESSED_DIR / f"hn_whos_hiring_{month}_matches.json"


def normalize_text(value: str) -> str:
    return " ".join(value.casefold().strip().split())


def slugify(value: Any, *, fallback: str = "unknown") -> str:
    text = normalize_text(as_string(value))
    text = re.sub(r"[^a-z0-9]+", "-", text)
    text = text.strip("-")
    return text or fallback


def stable_digest(value: str, length: int = 12) -> str:
    return hashlib.sha1(value.encode("utf-8")).hexdigest()[:length]


def company_key(company: Any) -> str:
    return slugify(company, fallback="unknown-company")


def opening_key_from_fields(
    *,
    company: Any,
    title: Any,
    apply_url: Any = None,
    apply_email: Any = None,
    company_url: Any = None,
    hn_url: Any = None,
    job_id: Any = None,
) -> str:
    """Build a stable but readable dedupe key for one opening."""
    company_part = slugify(company, fallback="unknown-company")
    title_part = slugify(title, fallback="unknown-role")
    contact = (
        as_string(apply_url)
        or as_string(apply_email)
        or as_string(company_url)
        or as_string(hn_url)
        or as_string(job_id)
        or "unknown-source"
    )
    basis = "|".join([normalize_text(company_part), normalize_text(title_part), normalize_text(contact)])
    return f"{company_part}--{title_part}--{stable_digest(basis)}"


def validate_month(month: str) -> None:
    if not VALID_MONTH_RE.match(month):
        raise SyncError("--month must use YYYY-MM format, e.g. 2026-07")


def comments_by_id(raw_data: JsonDict) -> dict[str, JsonDict]:
    comments = raw_data.get("comments")
    if not isinstance(comments, list):
        return {}
    result: dict[str, JsonDict] = {}
    for comment in comments:
        if isinstance(comment, dict):
            comment_id = as_string(comment.get("id"))
            if comment_id:
                result[comment_id] = comment
    return result


def extraction_entries(extracted_data: JsonDict) -> list[JsonDict]:
    items = extracted_data.get("items")
    if not isinstance(items, dict):
        raise SyncError("Extraction JSON is missing an 'items' object.")
    return [entry for entry in items.values() if isinstance(entry, dict)]


def flatten_jobs(extracted_data: JsonDict, raw_data: JsonDict) -> list[JsonDict]:
    comments = comments_by_id(raw_data)
    jobs: list[JsonDict] = []

    for entry in extraction_entries(extracted_data):
        extraction = entry.get("extraction")
        if not isinstance(extraction, dict) or not extraction.get("is_job_post"):
            continue
        roles = extraction.get("roles")
        if not isinstance(roles, list):
            continue

        comment_id = as_string(entry.get("comment_id"))
        comment = comments.get(comment_id, {})
        for role_index, role in enumerate(roles):
            if not isinstance(role, dict):
                continue
            jobs.append(
                {
                    "job_id": f"{comment_id}:{role_index}",
                    "comment_id": comment_id,
                    "role_index": role_index,
                    "company_name": extraction.get("company_name"),
                    "company_url": extraction.get("company_url"),
                    "company_description": extraction.get("company_description"),
                    "role": role,
                    "source_comment": {
                        "id": comment.get("id"),
                        "hn_url": comment.get("hn_url"),
                        "position_index": comment.get("position_index"),
                        "source_thread_title": comment.get("source_thread_title"),
                        "source_thread_id": comment.get("source_thread_id"),
                    },
                }
            )

    jobs.sort(
        key=lambda job: (
            as_number(job.get("source_comment", {}).get("position_index")) or 10**9,
            as_number(job.get("role_index")) or 0,
            as_string(job.get("company_name")),
            as_string((job.get("role") or {}).get("title") if isinstance(job.get("role"), dict) else ""),
        )
    )
    return jobs


def match_items(matches_data: JsonDict) -> dict[str, JsonDict]:
    items = matches_data.get("items")
    if not isinstance(items, dict):
        raise SyncError("Matches JSON is missing an 'items' object.")
    return {as_string(key): value for key, value in items.items() if isinstance(value, dict)}


def format_salary(role: JsonDict) -> str:
    salary_raw = as_string(role.get("salary_raw"))
    if salary_raw and salary_raw.casefold() != "not disclosed":
        return salary_raw

    salary_min = as_number(role.get("salary_min"))
    salary_max = as_number(role.get("salary_max"))
    currency = as_string(role.get("currency"))
    if salary_min is None and salary_max is None:
        return "Not disclosed"
    if salary_min is not None and salary_max is not None:
        return f"{currency} {salary_min:g}-{salary_max:g}".strip()
    if salary_min is not None:
        return f"{currency} {salary_min:g}+".strip()
    return f"{currency} up to {salary_max:g}".strip()


def format_location(role: JsonDict) -> str:
    parts = [
        as_string(role.get("remote_status")),
        as_string(role.get("remote_region")),
        as_string(role.get("location_raw")),
    ]
    return " / ".join(part for part in parts if part) or "Unknown"


def match_bucket(entry: JsonDict) -> str:
    hard_filter = entry.get("hard_filter") if isinstance(entry.get("hard_filter"), dict) else {}
    if hard_filter.get("rejected") or entry.get("match_status") == "hard_filtered":
        return "hard_filtered"
    if entry.get("match_status") == "not_llm_ranked" or entry.get("source") == "matching_budget_skipped":
        return "not_llm_ranked"
    match = entry.get("match") if isinstance(entry.get("match"), dict) else {}
    return as_string(match.get("bucket"), "skip")


def should_import_entry(entry: JsonDict | None, *, include_weak_fit: bool, include_skips: bool) -> bool:
    if not entry:
        return False
    bucket = match_bucket(entry)
    if bucket in DEFAULT_BUCKETS:
        return True
    if bucket == "weak_fit":
        return include_weak_fit
    return include_skips


def row_sort_key(row: JsonDict) -> tuple[int, float, str, str]:
    bucket_order = {
        "must_apply": 0,
        "strong_maybe": 1,
        "maybe": 2,
        "weak_fit": 3,
        "skip": 4,
        "not_llm_ranked": 5,
        "hard_filtered": 6,
    }
    score = as_number(row.get("score"))
    heuristic_score = as_number(row.get("heuristic_score"))
    best_score = score if score is not None else heuristic_score if heuristic_score is not None else -1
    return (
        bucket_order.get(as_string(row.get("bucket")), 9),
        -best_score,
        as_string(row.get("company")),
        as_string(row.get("title")),
    )


def crm_row_from_job(job: JsonDict, entry: JsonDict, *, month: str, synced_at: str) -> JsonDict:
    role = job.get("role") if isinstance(job.get("role"), dict) else {}
    source = job.get("source_comment") if isinstance(job.get("source_comment"), dict) else {}
    match = entry.get("match") if isinstance(entry.get("match"), dict) else {}
    hard_filter = entry.get("hard_filter") if isinstance(entry.get("hard_filter"), dict) else {}
    heuristic = entry.get("heuristic") if isinstance(entry.get("heuristic"), dict) else {}
    outreach = entry.get("outreach") if isinstance(entry.get("outreach"), dict) else {}

    company = as_string(job.get("company_name"), "Unknown company")
    title = as_string(role.get("title"), "Unknown role")
    apply_url = as_string(role.get("apply_url"))
    apply_email = as_string(role.get("apply_email"))
    hn_url = as_string(source.get("hn_url"))
    key = opening_key_from_fields(
        company=company,
        title=title,
        apply_url=apply_url,
        apply_email=apply_email,
        company_url=job.get("company_url"),
        hn_url=hn_url,
        job_id=job.get("job_id"),
    )

    bucket = match_bucket(entry)
    if bucket not in {"hard_filtered", "not_llm_ranked"}:
        bucket = as_string(match.get("bucket"), bucket or "skip")

    return {
        "opening_key": key,
        "company_key": company_key(company),
        "first_seen_month": month,
        "last_seen_month": month,
        "seen_months": month,
        "current_month_seen": "TRUE",
        "company_seen_before": "FALSE",
        "new_opening_for_existing_company": "FALSE",
        "job_id": as_string(job.get("job_id")),
        "comment_id": as_string(job.get("comment_id")),
        "company": company,
        "title": title,
        "bucket": bucket,
        "score": format_number(match.get("score")),
        "heuristic_score": format_number(heuristic.get("score")),
        "match_status": as_string(entry.get("match_status")),
        "location": format_location(role),
        "remote_status": as_string(role.get("remote_status")),
        "salary": format_salary(role),
        "tech_stack": join_csv(role.get("tech_stack")),
        "apply_url": apply_url,
        "apply_email": apply_email,
        "hn_url": hn_url,
        "summary": as_string(match.get("summary")),
        "positive_reasons": join_lines(match.get("positive_reasons")),
        "concerns": join_lines(match.get("concerns")),
        "missing_info": join_lines(match.get("missing_info")),
        "hard_filter_reasons": join_lines(hard_filter.get("reasons")),
        "draft_outreach_subject": as_string(outreach.get("subject")),
        "draft_outreach_body": as_string(outreach.get("body")),
        "last_synced_at": synced_at,
    }


def format_number(value: Any) -> str:
    number = as_number(value)
    if number is None:
        return ""
    if number.is_integer():
        return str(int(number))
    return f"{number:.1f}"


def source_rows_from_results(
    *,
    raw_data: JsonDict,
    extracted_data: JsonDict,
    matches_data: JsonDict,
    month: str,
    include_weak_fit: bool,
    include_skips: bool,
    synced_at: str,
) -> tuple[list[JsonDict], int, int]:
    matches = match_items(matches_data)
    rows: list[JsonDict] = []
    skipped = 0
    duplicate_openings = 0
    seen_opening_keys: set[str] = set()

    for job in flatten_jobs(extracted_data, raw_data):
        entry = matches.get(as_string(job.get("job_id")))
        if not should_import_entry(entry, include_weak_fit=include_weak_fit, include_skips=include_skips):
            skipped += 1
            continue
        assert entry is not None
        row = crm_row_from_job(job, entry, month=month, synced_at=synced_at)
        opening_key = as_string(row.get("opening_key"))
        if opening_key in seen_opening_keys:
            duplicate_openings += 1
            continue
        seen_opening_keys.add(opening_key)
        rows.append(row)

    rows.sort(key=row_sort_key)
    return rows, skipped, duplicate_openings


def split_months(value: Any) -> list[str]:
    months: list[str] = []
    for piece in re.split(r"[,\n;]+", as_string(value)):
        month = piece.strip()
        if month and month not in months:
            months.append(month)
    return months


def add_month(value: Any, month: str) -> str:
    months = split_months(value)
    if month not in months:
        months.append(month)
    return ", ".join(sorted(months))


def remove_month(value: Any, month: str) -> list[str]:
    return [item for item in split_months(value) if item != month]


def month_seen_by_row(row: JsonDict, month: str) -> bool:
    return (
        month in split_months(row.get("seen_months"))
        or as_string(row.get("first_seen_month")) == month
        or as_string(row.get("last_seen_month")) == month
    )


def remove_current_month_from_row(row: JsonDict, month: str) -> bool:
    remaining_months = remove_month(row.get("seen_months"), month)
    if not remaining_months:
        return False
    row["seen_months"] = ", ".join(sorted(remaining_months))
    row["first_seen_month"] = min(remaining_months)
    row["last_seen_month"] = max(remaining_months)
    row["current_month_seen"] = "FALSE"
    return True


def normalize_existing_row(row: JsonDict, header: list[str]) -> JsonDict:
    normalized = {column: as_string(row.get(column)) for column in header}
    if not normalized.get("company_key"):
        normalized["company_key"] = company_key(normalized.get("company"))
    if not normalized.get("opening_key"):
        normalized["opening_key"] = opening_key_from_fields(
            company=normalized.get("company"),
            title=normalized.get("title"),
            apply_url=normalized.get("apply_url"),
            apply_email=normalized.get("apply_email"),
            hn_url=normalized.get("hn_url"),
            job_id=normalized.get("job_id"),
        )
    return normalized


def merged_header(existing_header: list[str]) -> list[str]:
    return JOBS_HEADER


def all_known_columns() -> list[str]:
    columns: list[str] = []
    for column in AUTO_COLUMNS + MANUAL_COLUMNS + JOBS_HEADER + DETAILS_COLUMNS:
        if column not in columns:
            columns.append(column)
    return columns


def merge_rows(
    *,
    existing_rows: list[JsonDict],
    existing_header: list[str],
    source_rows: list[JsonDict],
    month: str,
    synced_at: str,
    replace_month: bool = False,
) -> tuple[list[JsonDict], JsonDict, list[str]]:
    header = merged_header(existing_header)
    internal_columns = all_known_columns()
    source_opening_keys = {as_string(row.get("opening_key")) for row in source_rows if as_string(row.get("opening_key"))}
    merged: list[JsonDict] = []
    rows_removed_for_month = 0
    rows_detached_from_month = 0
    for existing in existing_rows:
        normalized = normalize_existing_row(existing, internal_columns)
        if replace_month and month_seen_by_row(normalized, month) and as_string(normalized.get("opening_key")) not in source_opening_keys:
            if remove_current_month_from_row(normalized, month):
                rows_detached_from_month += 1
                merged.append(normalized)
            else:
                rows_removed_for_month += 1
            continue
        merged.append(normalized)

    existing_by_opening: dict[str, int] = {}
    existing_company_keys: set[str] = set()
    openings_by_company: dict[str, set[str]] = defaultdict(set)

    for index, row in enumerate(merged):
        key = as_string(row.get("opening_key"))
        ckey = as_string(row.get("company_key"))
        if key and key not in existing_by_opening:
            existing_by_opening[key] = index
        if ckey:
            existing_company_keys.add(ckey)
            if key:
                openings_by_company[ckey].add(key)
        row["current_month_seen"] = "FALSE"
        row["last_synced_at"] = synced_at

    added = 0
    updated = 0
    companies_seen_before = 0
    new_openings_for_existing_companies = 0

    for source in source_rows:
        key = as_string(source.get("opening_key"))
        ckey = as_string(source.get("company_key"))
        if not key:
            continue

        if key in existing_by_opening:
            row = merged[existing_by_opening[key]]
            first_seen = as_string(row.get("first_seen_month")) or month
            seen_months = add_month(row.get("seen_months"), month)
            for column in AUTO_COLUMNS:
                row[column] = as_string(source.get(column))
            row["first_seen_month"] = first_seen
            row["seen_months"] = seen_months
            row["last_seen_month"] = month
            row["current_month_seen"] = "TRUE"
            row["company_seen_before"] = "TRUE"
            row["new_opening_for_existing_company"] = "FALSE"
            updated += 1
            continue

        row = {column: "" for column in internal_columns}
        for column in AUTO_COLUMNS:
            row[column] = as_string(source.get(column))
        row["first_seen_month"] = month
        row["last_seen_month"] = month
        row["seen_months"] = month
        row["current_month_seen"] = "TRUE"

        prior_company_openings = openings_by_company.get(ckey, set())
        company_seen = ckey in existing_company_keys
        is_new_opening_for_existing_company = company_seen and key not in prior_company_openings
        row["company_seen_before"] = bool_text(company_seen)
        row["new_opening_for_existing_company"] = bool_text(is_new_opening_for_existing_company)
        if company_seen:
            companies_seen_before += 1
        if is_new_opening_for_existing_company:
            new_openings_for_existing_companies += 1

        existing_by_opening[key] = len(merged)
        existing_company_keys.add(ckey)
        openings_by_company[ckey].add(key)
        merged.append(row)
        added += 1

    stats = {
        "rows_added": added,
        "rows_updated": updated,
        "companies_seen_before": companies_seen_before,
        "new_openings_for_existing_companies": new_openings_for_existing_companies,
        "rows_removed_for_month": rows_removed_for_month,
        "rows_detached_from_month": rows_detached_from_month,
        "total_rows_after_sync": len(merged),
    }
    return merged, stats, header


def bool_text(value: bool) -> str:
    return "TRUE" if value else "FALSE"


def sheet_cell(value: Any) -> str:
    text = as_string(value)
    if text.startswith(("=", "+", "-", "@")):
        return "'" + text
    return text


def row_values(rows: list[JsonDict], header: list[str]) -> list[list[str]]:
    return [[sheet_cell(row.get(column)) for column in header] for row in rows]


def records_from_values(values: list[list[Any]]) -> tuple[list[str], list[JsonDict]]:
    if not values:
        return [], []
    header = [as_string(value) for value in values[0]]
    rows: list[JsonDict] = []
    for raw_row in values[1:]:
        row = {}
        for index, column in enumerate(header):
            if not column:
                continue
            row[column] = as_string(raw_row[index]) if index < len(raw_row) else ""
        if any(row.values()):
            rows.append(row)
    return header, rows


def truthy(value: Any) -> bool:
    return normalize_text(as_string(value)) in {"true", "yes", "y", "1", "needed", "follow up", "follow-up"}


def parse_iso_date(value: Any) -> date | None:
    text = as_string(value)
    if not text:
        return None
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        return None


def followup_due(row: JsonDict, today: date) -> bool:
    if truthy(row.get("followup_needed")):
        return True
    followup_date = parse_iso_date(row.get("followup_date"))
    return bool(followup_date and followup_date <= today)


def build_followup_rows(jobs_rows: list[JsonDict], today: date) -> list[JsonDict]:
    followups = [row for row in jobs_rows if followup_due(row, today)]
    followups.sort(
        key=lambda row: (
            parse_iso_date(row.get("followup_date")) or date.max,
            as_string(row.get("company")),
            as_string(row.get("title")),
        )
    )
    return [{column: as_string(row.get(column)) for column in FOLLOWUP_COLUMNS} for row in followups]


def build_details_rows(jobs_rows: list[JsonDict]) -> list[JsonDict]:
    rows = [{column: as_string(row.get(column)) for column in DETAILS_COLUMNS} for row in jobs_rows]
    rows.sort(
        key=lambda row: (
            as_string(row.get("company")),
            as_string(row.get("title")),
            as_string(row.get("opening_key")),
        )
    )
    return rows


def readme_rows() -> list[list[str]]:
    return [
        ["HN Hiring Scout CRM", "This workbook is maintained by sync_google_sheet.py."],
        ["Jobs tab", "Compact working CRM view. Company is first; fit signals and application workflow come next."],
        ["Details tab", "Long AI summaries, fit reasons, concerns, missing info, and draft outreach live here."],
        ["Hidden/internal columns", ", ".join(INTERNAL_COLUMNS)],
        ["Housekeeping columns", ", ".join(HOUSEKEEPING_COLUMNS)],
        ["Manual CRM columns", ", ".join([column for column in MANUAL_COLUMNS if column in JOBS_HEADER])],
        [
            "Sync behavior",
            "The script updates rows by hidden opening_key, preserves manual CRM columns, and appends new openings.",
        ],
        [
            "Duplicate policy",
            "opening_key is derived from normalized company, role title, and apply URL/email/company URL fallback. It is hidden at the far right of Jobs.",
        ],
        [
            "Monthly tracking",
            "The first_seen/last_seen/seen_months/current_month columns are housekeeping and are kept near the end of Jobs.",
        ],
        [
            "Followups tab",
            "Generated from Jobs rows where followup_needed is TRUE/yes or followup_date is due. Edit follow-up fields on Jobs, not Followups.",
        ],
        [
            "Repopulate a month",
            "Use --replace-month to remove stale rows from the current month before re-adding the latest report rows.",
        ],
        [
            "Do not edit",
            "Edit application workflow columns on Jobs. Avoid editing hidden/internal columns unless you intentionally want to repair CRM metadata.",
        ],
    ]


def make_gspread_client(credentials_file: Path) -> Any:
    try:
        import gspread
        from google.oauth2.service_account import Credentials
    except ImportError as exc:
        raise SyncError(
            "Google Sheets dependencies are missing. Run: pip install -r requirements.txt"
        ) from exc

    if not credentials_file.exists():
        raise SyncError(f"Google service account credentials file not found: {credentials_file}")
    credentials = Credentials.from_service_account_file(str(credentials_file), scopes=SCOPES)
    return gspread.authorize(credentials)


def get_or_create_worksheet(spreadsheet: Any, title: str, rows: int, cols: int) -> Any:
    import gspread

    try:
        return spreadsheet.worksheet(title)
    except gspread.exceptions.WorksheetNotFound:
        return spreadsheet.add_worksheet(title=title, rows=max(rows, 100), cols=max(cols, 20))


def get_worksheet_or_none(spreadsheet: Any, title: str) -> Any | None:
    import gspread

    try:
        return spreadsheet.worksheet(title)
    except gspread.exceptions.WorksheetNotFound:
        return None


def worksheet_values(worksheet: Any) -> list[list[Any]]:
    values = worksheet.get_all_values()
    return values if isinstance(values, list) else []


def update_worksheet(worksheet: Any, values: list[list[str]]) -> None:
    worksheet.clear()
    if not values:
        return
    rows = max(len(values), 1)
    cols = max(len(values[0]), 1)
    worksheet.resize(rows=rows, cols=cols)
    try:
        worksheet.update(values=values, range_name="A1", value_input_option="USER_ENTERED")
    except TypeError:
        worksheet.update("A1", values, value_input_option="USER_ENTERED")
    try:
        worksheet.freeze(rows=1)
    except Exception:
        pass


def column_letter(index: int) -> str:
    """Return an A1 column letter for a 1-based column index."""
    result = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        result = chr(65 + remainder) + result
    return result


def format_range_for_columns(header: list[str], first_column: str, last_column: str, row_count: int) -> str | None:
    if first_column not in header or last_column not in header:
        return None
    start = header.index(first_column) + 1
    end = header.index(last_column) + 1
    return f"{column_letter(start)}1:{column_letter(end)}{max(row_count, 2)}"


def safe_format(worksheet: Any, range_name: str, cell_format: JsonDict) -> None:
    try:
        worksheet.format(range_name, cell_format)
    except Exception:
        pass


def safe_set_basic_filter(worksheet: Any) -> None:
    try:
        worksheet.set_basic_filter()
    except Exception:
        pass


def hide_columns_by_name(worksheet: Any, header: list[str], names: list[str]) -> None:
    requests = []
    for name in names:
        if name not in header:
            continue
        index = header.index(name)
        requests.append(
            {
                "updateDimensionProperties": {
                    "range": {
                        "sheetId": worksheet.id,
                        "dimension": "COLUMNS",
                        "startIndex": index,
                        "endIndex": index + 1,
                    },
                    "properties": {"hiddenByUser": True},
                    "fields": "hiddenByUser",
                }
            }
        )
    if not requests:
        return
    try:
        worksheet.spreadsheet.batch_update({"requests": requests})
    except Exception:
        pass


def set_column_widths(worksheet: Any, widths: dict[str, int]) -> None:
    requests = []
    for column_name, width in widths.items():
        requests.append(
            {
                "updateDimensionProperties": {
                    "range": {
                        "sheetId": worksheet.id,
                        "dimension": "COLUMNS",
                        "startIndex": column_index_from_letter(column_name) - 1,
                        "endIndex": column_index_from_letter(column_name),
                    },
                    "properties": {"pixelSize": width},
                    "fields": "pixelSize",
                }
            }
        )
    try:
        worksheet.spreadsheet.batch_update({"requests": requests})
    except Exception:
        pass


def column_index_from_letter(letters: str) -> int:
    result = 0
    for char in letters.upper():
        if not ("A" <= char <= "Z"):
            continue
        result = result * 26 + ord(char) - 64
    return result


def style_jobs_sheet(worksheet: Any, header: list[str], row_count: int) -> None:
    last_column = column_letter(len(header))
    safe_format(
        worksheet,
        f"A1:{last_column}1",
        {
            "backgroundColor": {"red": 0.08, "green": 0.17, "blue": 0.31},
            "textFormat": {"foregroundColor": {"red": 1, "green": 1, "blue": 1}, "bold": True},
            "horizontalAlignment": "CENTER",
        },
    )
    safe_format(worksheet, f"A1:{last_column}{max(row_count, 2)}", {"wrapStrategy": "WRAP"})
    safe_format(worksheet, f"D2:E{max(row_count, 2)}", {"horizontalAlignment": "CENTER"})

    app_range = format_range_for_columns(header, "application_status", "do_not_apply", row_count)
    if app_range:
        safe_format(worksheet, app_range, {"backgroundColor": {"red": 1.0, "green": 0.97, "blue": 0.82}})

    links_range = format_range_for_columns(header, "apply_url", "hn_url", row_count)
    if links_range:
        safe_format(worksheet, links_range, {"backgroundColor": {"red": 0.9, "green": 0.96, "blue": 1.0}})

    housekeeping_range = format_range_for_columns(header, "first_seen_month", "new_opening_for_existing_company", row_count)
    if housekeeping_range:
        safe_format(worksheet, housekeeping_range, {"backgroundColor": {"red": 0.94, "green": 0.94, "blue": 0.94}})

    safe_set_basic_filter(worksheet)
    hide_columns_by_name(worksheet, header, INTERNAL_COLUMNS)
    set_column_widths(
        worksheet,
        {
            "A": 180,
            "B": 220,
            "C": 120,
            "D": 70,
            "E": 105,
            "F": 220,
            "G": 110,
            "H": 130,
            "I": 240,
            "J": 150,
            "K": 120,
            "L": 150,
            "P": 120,
            "Q": 220,
            "T": 260,
            "V": 220,
            "W": 180,
            "X": 220,
        },
    )


def style_details_sheet(worksheet: Any, header: list[str], row_count: int) -> None:
    last_column = column_letter(len(header))
    safe_format(
        worksheet,
        f"A1:{last_column}1",
        {
            "backgroundColor": {"red": 0.18, "green": 0.29, "blue": 0.21},
            "textFormat": {"foregroundColor": {"red": 1, "green": 1, "blue": 1}, "bold": True},
            "horizontalAlignment": "CENTER",
        },
    )
    safe_format(worksheet, f"A1:{last_column}{max(row_count, 2)}", {"wrapStrategy": "WRAP"})
    safe_set_basic_filter(worksheet)
    hide_columns_by_name(worksheet, header, ["opening_key"])
    set_column_widths(
        worksheet,
        {
            "A": 180,
            "B": 220,
            "F": 320,
            "G": 320,
            "H": 320,
            "I": 260,
            "K": 240,
            "L": 360,
            "N": 220,
            "P": 220,
        },
    )


def style_followups_sheet(worksheet: Any, header: list[str], row_count: int) -> None:
    last_column = column_letter(len(header))
    safe_format(
        worksheet,
        f"A1:{last_column}1",
        {
            "backgroundColor": {"red": 0.42, "green": 0.19, "blue": 0.09},
            "textFormat": {"foregroundColor": {"red": 1, "green": 1, "blue": 1}, "bold": True},
            "horizontalAlignment": "CENTER",
        },
    )
    safe_format(worksheet, f"A1:{last_column}{max(row_count, 2)}", {"wrapStrategy": "WRAP"})
    safe_set_basic_filter(worksheet)
    set_column_widths(worksheet, {"A": 180, "B": 220, "C": 120, "D": 260, "L": 260})


def style_readme_sheet(worksheet: Any, row_count: int) -> None:
    safe_format(
        worksheet,
        f"A1:B{max(row_count, 2)}",
        {"wrapStrategy": "WRAP"},
    )
    safe_format(
        worksheet,
        "A1:B1",
        {
            "backgroundColor": {"red": 0.08, "green": 0.17, "blue": 0.31},
            "textFormat": {"foregroundColor": {"red": 1, "green": 1, "blue": 1}, "bold": True},
        },
    )
    set_column_widths(worksheet, {"A": 220, "B": 680})


def open_sheet(spreadsheet_id: str, credentials_file: Path) -> Any:
    client = make_gspread_client(credentials_file)
    try:
        return client.open_by_key(spreadsheet_id)
    except Exception as exc:
        raise SyncError(
            "Could not open Google Sheet. Confirm the spreadsheet ID is correct and the sheet "
            "is shared with the service account email."
        ) from exc


def load_existing_jobs_from_sheet(
    spreadsheet_id: str,
    credentials_file: Path,
    *,
    create_missing: bool,
) -> tuple[list[str], list[JsonDict]]:
    spreadsheet = open_sheet(spreadsheet_id, credentials_file)
    if create_missing:
        worksheet = get_or_create_worksheet(spreadsheet, JOBS_SHEET_NAME, rows=100, cols=len(JOBS_HEADER))
    else:
        worksheet = get_worksheet_or_none(spreadsheet, JOBS_SHEET_NAME)
        if worksheet is None:
            return [], []
    jobs_header, jobs_rows = records_from_values(worksheet_values(worksheet))

    details_sheet = get_worksheet_or_none(spreadsheet, DETAILS_SHEET_NAME)
    if details_sheet is None:
        return jobs_header, jobs_rows

    _, details_rows = records_from_values(worksheet_values(details_sheet))
    details_by_key = {
        as_string(row.get("opening_key")): row
        for row in details_rows
        if as_string(row.get("opening_key"))
    }
    for row in jobs_rows:
        details = details_by_key.get(as_string(row.get("opening_key")))
        if not details:
            continue
        for column in DETAILS_COLUMNS:
            if column not in row or not as_string(row.get(column)):
                row[column] = as_string(details.get(column))
    return jobs_header, jobs_rows


def write_sync_to_sheet(
    *,
    spreadsheet_id: str,
    credentials_file: Path,
    jobs_rows: list[JsonDict],
    jobs_header: list[str],
) -> None:
    spreadsheet = open_sheet(spreadsheet_id, credentials_file)

    jobs_sheet = get_or_create_worksheet(
        spreadsheet,
        JOBS_SHEET_NAME,
        rows=len(jobs_rows) + 1,
        cols=len(jobs_header),
    )
    update_worksheet(jobs_sheet, [jobs_header] + row_values(jobs_rows, jobs_header))
    style_jobs_sheet(jobs_sheet, jobs_header, len(jobs_rows) + 1)

    details_rows = build_details_rows(jobs_rows)
    details_sheet = get_or_create_worksheet(
        spreadsheet,
        DETAILS_SHEET_NAME,
        rows=len(details_rows) + 1,
        cols=len(DETAILS_COLUMNS),
    )
    update_worksheet(
        details_sheet,
        [DETAILS_COLUMNS] + row_values(details_rows, DETAILS_COLUMNS),
    )
    style_details_sheet(details_sheet, DETAILS_COLUMNS, len(details_rows) + 1)

    followup_rows = build_followup_rows(jobs_rows, date.today())
    followups_sheet = get_or_create_worksheet(
        spreadsheet,
        FOLLOWUPS_SHEET_NAME,
        rows=len(followup_rows) + 1,
        cols=len(FOLLOWUP_COLUMNS),
    )
    update_worksheet(
        followups_sheet,
        [FOLLOWUP_COLUMNS] + row_values(followup_rows, FOLLOWUP_COLUMNS),
    )
    style_followups_sheet(followups_sheet, FOLLOWUP_COLUMNS, len(followup_rows) + 1)

    readme_sheet = get_or_create_worksheet(spreadsheet, README_SHEET_NAME, rows=20, cols=2)
    readme = readme_rows()
    update_worksheet(readme_sheet, readme)
    style_readme_sheet(readme_sheet, len(readme))


def print_summary(stats: JsonDict) -> None:
    print("")
    print("Google Sheets CRM sync summary")
    for key in [
        "month",
        "source_rows",
        "rows_added",
        "rows_updated",
        "companies_seen_before",
        "new_openings_for_existing_companies",
        "rows_removed_for_month",
        "rows_detached_from_month",
        "skipped_rows",
        "duplicate_source_openings",
        "total_rows_after_sync",
    ]:
        print(f"- {key.replace('_', ' ')}: {stats.get(key, 0)}")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Sync HN Hiring Scout processed results into a Google Sheets CRM.",
    )
    parser.add_argument("--month", help="Month to sync in YYYY-MM format, e.g. 2026-07.")
    parser.add_argument("--spreadsheet-id", help="Google Sheet ID. Defaults to GOOGLE_SHEET_ID.")
    parser.add_argument(
        "--credentials-file",
        type=Path,
        help="Google service account JSON file. Defaults to GOOGLE_SHEETS_CREDENTIALS_FILE.",
    )
    parser.add_argument("--raw-file", type=Path, help="Override raw HN JSON file path.")
    parser.add_argument("--extracted-file", type=Path, help="Override extracted jobs JSON file path.")
    parser.add_argument("--matches-file", type=Path, help="Override matches JSON file path.")
    parser.add_argument("--dry-run", action="store_true", help="Compute changes without writing to Google Sheets.")
    parser.add_argument(
        "--replace-month",
        action="store_true",
        help=(
            "Repopulate this month by removing stale rows previously associated with --month "
            "before writing the latest report rows. Matching openings keep manual CRM fields."
        ),
    )
    parser.add_argument("--include-weak-fit", action="store_true", help="Include weak_fit jobs in the CRM sync.")
    parser.add_argument(
        "--include-skips",
        action="store_true",
        help="Include skip, hard-filtered, and not-LLM-ranked jobs in the CRM sync.",
    )
    parser.add_argument("--self-test", action="store_true", help="Run local unit tests and exit.")
    return parser.parse_args(argv)


def resolve_credentials_file(args: argparse.Namespace) -> Path | None:
    if args.credentials_file:
        return args.credentials_file.expanduser()
    env_path = os.getenv("GOOGLE_SHEETS_CREDENTIALS_FILE") or os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
    return Path(env_path).expanduser() if env_path else None


def run_sync(args: argparse.Namespace) -> None:
    if load_dotenv:
        load_dotenv()

    if args.self_test:
        raise SystemExit(run_self_test())

    if not args.month:
        raise SyncError("--month is required unless --self-test is used.")
    validate_month(args.month)

    spreadsheet_id = args.spreadsheet_id or os.getenv("GOOGLE_SHEET_ID")
    credentials_file = resolve_credentials_file(args)

    if not args.dry_run:
        if not spreadsheet_id:
            raise SyncError("--spreadsheet-id or GOOGLE_SHEET_ID is required.")
        if not credentials_file:
            raise SyncError("--credentials-file or GOOGLE_SHEETS_CREDENTIALS_FILE is required.")

    raw_path = args.raw_file or default_raw_path(args.month)
    extracted_path = args.extracted_file or default_extracted_path(args.month)
    matches_path = args.matches_file or default_matches_path(args.month)

    raw_data = read_json_file(raw_path)
    extracted_data = read_json_file(extracted_path)
    matches_data = read_json_file(matches_path)
    synced_at = utc_now_iso()

    source_rows, skipped_rows, duplicate_source_openings = source_rows_from_results(
        raw_data=raw_data,
        extracted_data=extracted_data,
        matches_data=matches_data,
        month=args.month,
        include_weak_fit=args.include_weak_fit,
        include_skips=args.include_skips,
        synced_at=synced_at,
    )

    existing_header: list[str] = []
    existing_rows: list[JsonDict] = []
    if not args.dry_run and spreadsheet_id and credentials_file:
        existing_header, existing_rows = load_existing_jobs_from_sheet(
            spreadsheet_id,
            credentials_file,
            create_missing=True,
        )
    elif args.dry_run:
        print("Dry run: simulating against an empty CRM sheet. No Google API calls will be made.")

    merged_rows, merge_stats, header = merge_rows(
        existing_rows=existing_rows,
        existing_header=existing_header,
        source_rows=source_rows,
        month=args.month,
        synced_at=synced_at,
        replace_month=args.replace_month,
    )

    stats = {
        "month": args.month,
        "source_rows": len(source_rows),
        "skipped_rows": skipped_rows,
        "duplicate_source_openings": duplicate_source_openings,
        **merge_stats,
    }
    print_summary(stats)

    if args.dry_run:
        print("")
        print("Dry run complete. No Google Sheets writes were made.")
        return

    assert spreadsheet_id is not None
    assert credentials_file is not None
    write_sync_to_sheet(
        spreadsheet_id=spreadsheet_id,
        credentials_file=credentials_file,
        jobs_rows=merged_rows,
        jobs_header=header,
    )
    print("")
    print(f"Updated Google Sheet: https://docs.google.com/spreadsheets/d/{spreadsheet_id}")


def check(name: str, passed: bool, detail: str = "") -> tuple[str, bool, str]:
    return name, passed, detail


def run_self_test() -> int:
    tests: list[tuple[str, bool, str]] = []

    key_a = opening_key_from_fields(
        company="Example Inc.",
        title="Senior Backend Engineer",
        apply_url="https://example.com/jobs/1",
    )
    key_b = opening_key_from_fields(
        company=" example inc ",
        title="Senior Backend Engineer",
        apply_url="https://example.com/jobs/1",
    )
    key_c = opening_key_from_fields(
        company="Example Inc.",
        title="Staff Platform Engineer",
        apply_url="https://example.com/jobs/1",
    )
    key_d = opening_key_from_fields(
        company="Example Inc.",
        title="Senior Backend Engineer",
        company_url="https://example.com",
    )
    tests.append(check("same opening key is stable", key_a == key_b, f"{key_a} vs {key_b}"))
    tests.append(check("new role gets different opening key", key_a != key_c, f"{key_a} vs {key_c}"))
    tests.append(check("missing apply URL falls back safely", bool(key_d), key_d))
    tests.append(check("jobs header starts with company", JOBS_HEADER[0] == "company", str(JOBS_HEADER[:5])))
    tests.append(
        check(
            "housekeeping columns are near end",
            JOBS_HEADER.index("first_seen_month") > JOBS_HEADER.index("hn_url"),
            str(JOBS_HEADER),
        )
    )

    old_key = opening_key_from_fields(company="OldCo", title="Backend Engineer", apply_url="https://old/jobs/1")
    existing_key = opening_key_from_fields(
        company="Example Inc.",
        title="Senior Backend Engineer",
        apply_url="https://example.com/jobs/1",
    )
    new_key_same_company = opening_key_from_fields(
        company="Example Inc.",
        title="Principal Engineer",
        apply_url="https://example.com/jobs/2",
    )
    existing_rows = [
        {
            "opening_key": existing_key,
            "company_key": company_key("Example Inc."),
            "first_seen_month": "2026-06",
            "last_seen_month": "2026-06",
            "seen_months": "2026-06",
            "current_month_seen": "TRUE",
            "company": "Example Inc.",
            "title": "Senior Backend Engineer",
            "application_status": "Applied",
            "notes": "Manual note must survive.",
        },
        {
            "opening_key": old_key,
            "company_key": company_key("OldCo"),
            "first_seen_month": "2026-06",
            "last_seen_month": "2026-06",
            "seen_months": "2026-06",
            "current_month_seen": "TRUE",
            "company": "OldCo",
            "title": "Backend Engineer",
        },
    ]
    source_rows = [
        {
            "opening_key": existing_key,
            "company_key": company_key("Example Inc."),
            "company": "Example Inc.",
            "title": "Senior Backend Engineer",
            "score": "90",
            "last_synced_at": "now",
        },
        {
            "opening_key": new_key_same_company,
            "company_key": company_key("Example Inc."),
            "company": "Example Inc.",
            "title": "Principal Engineer",
            "score": "88",
            "last_synced_at": "now",
        },
    ]
    merged, stats, _ = merge_rows(
        existing_rows=existing_rows,
        existing_header=JOBS_HEADER,
        source_rows=source_rows,
        month="2026-07",
        synced_at="now",
    )
    by_key = {row["opening_key"]: row for row in merged}
    tests.append(
        check(
            "manual application fields preserved",
            by_key[existing_key].get("application_status") == "Applied"
            and by_key[existing_key].get("notes") == "Manual note must survive.",
            str(by_key[existing_key]),
        )
    )
    tests.append(
        check(
            "auto fields update on rerun",
            by_key[existing_key].get("score") == "90"
            and by_key[existing_key].get("last_seen_month") == "2026-07"
            and by_key[existing_key].get("seen_months") == "2026-06, 2026-07",
            str(by_key[existing_key]),
        )
    )
    tests.append(
        check(
            "old rows become current_month_seen false",
            by_key[old_key].get("current_month_seen") == "FALSE",
            str(by_key[old_key]),
        )
    )
    tests.append(
        check(
            "new opening for existing company is marked",
            by_key[new_key_same_company].get("company_seen_before") == "TRUE"
            and by_key[new_key_same_company].get("new_opening_for_existing_company") == "TRUE",
            str(by_key[new_key_same_company]),
        )
    )
    tests.append(check("merge stats count added", stats.get("rows_added") == 1, str(stats)))
    tests.append(check("merge stats count updated", stats.get("rows_updated") == 1, str(stats)))

    replace_existing_rows = [
        {
            "opening_key": existing_key,
            "company_key": company_key("Example Inc."),
            "first_seen_month": "2026-07",
            "last_seen_month": "2026-07",
            "seen_months": "2026-07",
            "company": "Example Inc.",
            "title": "Senior Backend Engineer",
        },
        {
            "opening_key": old_key,
            "company_key": company_key("OldCo"),
            "first_seen_month": "2026-07",
            "last_seen_month": "2026-07",
            "seen_months": "2026-07",
            "company": "OldCo",
            "title": "Backend Engineer",
        },
    ]
    replaced, replace_stats, _ = merge_rows(
        existing_rows=replace_existing_rows,
        existing_header=JOBS_HEADER,
        source_rows=[source_rows[0]],
        month="2026-07",
        synced_at="now",
        replace_month=True,
    )
    tests.append(
        check(
            "replace-month removes stale current report rows",
            old_key not in {row.get("opening_key") for row in replaced}
            and replace_stats.get("rows_removed_for_month") == 1,
            f"rows={replaced!r} stats={replace_stats!r}",
        )
    )

    failed = 0
    for name, passed, detail in tests:
        status = "PASS" if passed else "FAIL"
        suffix = f" - {detail}" if detail and not passed else ""
        print(f"{status} {name}{suffix}")
        if not passed:
            failed += 1
    print(f"{len(tests) - failed}/{len(tests)} self-tests passed.")
    return 1 if failed else 0


def main() -> None:
    try:
        run_sync(parse_args())
    except SyncError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
