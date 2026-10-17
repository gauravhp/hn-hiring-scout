#!/usr/bin/env python3
"""Analyze fetched Hacker News Who is Hiring comments with an LLM."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - exercised only in incomplete environments.
    load_dotenv = None  # type: ignore[assignment]

try:
    from openai import OpenAI
except ImportError:  # pragma: no cover - exercised only in incomplete environments.
    OpenAI = None  # type: ignore[assignment,misc]

try:
    from tqdm import tqdm
except ImportError:  # pragma: no cover - exercised only in incomplete environments.
    tqdm = None  # type: ignore[assignment,misc]

try:
    import yaml
except ImportError:  # pragma: no cover - exercised only in incomplete environments.
    yaml = None  # type: ignore[assignment]


JsonDict = dict[str, Any]

RAW_DIR = Path("data/raw")
PROCESSED_DIR = Path("data/processed")
REPORTS_DIR = Path("data/reports")
EXTRACTION_CACHE_VERSION = 1
MATCH_CACHE_VERSION = 1
VALID_REMOTE_STATUSES = {"remote", "hybrid", "onsite", "unknown"}
VALID_CONFIDENCE = {"low", "medium", "high"}
VALID_BUCKETS = {"must_apply", "strong_maybe", "maybe", "weak_fit", "skip"}


class AnalyzeError(RuntimeError):
    """Raised for user-facing analyzer errors."""


class LLMJsonParseError(AnalyzeError):
    """Raised when an LLM response cannot be parsed as JSON."""

    def __init__(self, message: str, *, raw_content: str) -> None:
        super().__init__(message)
        self.raw_content = raw_content


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Extract, filter, rank, and report on HN Who is Hiring jobs."
    )
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="Run parser and hard-filter self-tests, then exit.",
    )
    parser.add_argument(
        "--month",
        help="Report month in YYYY-MM format, for example 2026-07.",
    )
    parser.add_argument(
        "--profile",
        type=Path,
        help="Path to profile YAML, for example profile.yaml.",
    )
    parser.add_argument(
        "--raw-file",
        type=Path,
        help="Optional explicit raw JSON file. Defaults to data/raw/hn_whos_hiring_{month}_raw.json.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        help="Limit raw comments before prefiltering. Useful for testing.",
    )
    parser.add_argument(
        "--max-extractions",
        type=int,
        help="Limit LLM extraction candidates after prefiltering, sorted by prefilter score.",
    )
    parser.add_argument(
        "--top-n-for-matching",
        type=int,
        help="Limit new LLM matching calls after hard filters and heuristic scoring.",
    )
    parser.add_argument(
        "--force-extract",
        action="store_true",
        help="Ignore matching extraction cache entries and re-extract comments.",
    )
    parser.add_argument(
        "--force-match",
        action="store_true",
        help="Ignore matching job score cache entries and re-score jobs.",
    )
    parser.add_argument(
        "--clear-analysis",
        action="store_true",
        help=(
            "Delete this month's prefilter, extraction, match, and report outputs before "
            "running. The raw HN input is preserved."
        ),
    )
    parser.add_argument(
        "--generate-outreach",
        action="store_true",
        help="Generate cached outreach emails for must-apply or score >= 85 jobs.",
    )
    parser.add_argument(
        "--report-mode",
        choices=("shortlist", "full"),
        default="shortlist",
        help="Report detail level. shortlist is concise; full preserves detailed output.",
    )
    parser.add_argument(
        "--include-deprioritized",
        action="store_true",
        help="Include deprioritized prefilter comments in LLM extraction.",
    )
    parser.add_argument(
        "--prefilter-only",
        action="store_true",
        help="Run comment-level prefiltering, write results, and exit before LLM calls.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run without LLM calls or LLM environment variables using heuristic outputs.",
    )
    parser.add_argument(
        "--show-heuristic-details",
        action="store_true",
        help="Print the top 50 eligible jobs by heuristic score with positive and negative signals.",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Proceed without confirmation when planned new LLM calls exceed the safety threshold.",
    )
    args = parser.parse_args()
    if not args.self_test:
        if not args.month:
            parser.error("--month is required unless --self-test is used.")
        if not args.profile:
            parser.error("--profile is required unless --self-test is used.")
        if args.limit is not None and args.limit <= 0:
            parser.error("--limit must be greater than 0.")
        if args.max_extractions is not None and args.max_extractions <= 0:
            parser.error("--max-extractions must be greater than 0.")
        if args.top_n_for_matching is not None and args.top_n_for_matching <= 0:
            parser.error("--top-n-for-matching must be greater than 0.")
    return args


def ensure_dependencies(*, dry_run: bool) -> None:
    missing: list[str] = []
    if load_dotenv is None:
        missing.append("python-dotenv")
    if tqdm is None:
        missing.append("tqdm")
    if yaml is None:
        missing.append("PyYAML")
    if not dry_run and OpenAI is None:
        missing.append("openai")

    if missing:
        raise AnalyzeError(
            "Missing required package(s): "
            + ", ".join(missing)
            + ". Install dependencies with: python3 -m pip install -r requirements.txt"
        )


def validate_month(month: str) -> None:
    if not re.fullmatch(r"\d{4}-\d{2}", month):
        raise AnalyzeError(f"Invalid --month '{month}'. Expected format YYYY-MM.")
    try:
        datetime.strptime(month, "%Y-%m")
    except ValueError as exc:
        raise AnalyzeError(f"Invalid --month '{month}'. Expected a real calendar month.") from exc


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def default_raw_path(month: str) -> Path:
    return RAW_DIR / f"hn_whos_hiring_{month}_raw.json"


def extraction_cache_path(month: str) -> Path:
    return PROCESSED_DIR / f"hn_whos_hiring_{month}_extracted.json"


def prefilter_cache_path(month: str) -> Path:
    return PROCESSED_DIR / f"hn_whos_hiring_{month}_prefiltered.json"


def match_cache_path(month: str) -> Path:
    return PROCESSED_DIR / f"hn_whos_hiring_{month}_matches.json"


def report_path(month: str) -> Path:
    return REPORTS_DIR / f"hn_whos_hiring_{month}_report.md"


def analysis_output_paths(month: str) -> list[Path]:
    """Return the generated artifacts that can safely be rebuilt from raw input."""
    return [
        prefilter_cache_path(month),
        extraction_cache_path(month),
        match_cache_path(month),
        report_path(month),
    ]


def clear_analysis_outputs(month: str) -> list[Path]:
    """Remove generated monthly artifacts while intentionally preserving raw input."""
    removed: list[Path] = []
    for path in analysis_output_paths(month):
        if path.exists():
            path.unlink()
            removed.append(path)
    return removed


def read_json_file(path: Path) -> JsonDict:
    if not path.exists():
        raise AnalyzeError(f"JSON file does not exist: {path}")
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except json.JSONDecodeError as exc:
        raise AnalyzeError(f"Invalid JSON in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise AnalyzeError(f"Expected top-level JSON object in {path}.")
    return data


def write_json_file(path: Path, data: JsonDict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2, ensure_ascii=False)
        handle.write("\n")


def read_text_file(path: Path) -> str:
    if not path.exists():
        raise AnalyzeError(f"File does not exist: {path}")
    return path.read_text(encoding="utf-8")


def load_profile(path: Path) -> JsonDict:
    if yaml is None:
        raise AnalyzeError("Missing required package: PyYAML.")
    try:
        data = yaml.safe_load(read_text_file(path))
    except yaml.YAMLError as exc:  # type: ignore[union-attr]
        raise AnalyzeError(f"Invalid YAML in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise AnalyzeError(f"Expected top-level YAML mapping in {path}.")
    return data


def load_cache(path: Path, *, version: int, month: str) -> JsonDict:
    if not path.exists():
        return {
            "cache_version": version,
            "month": month,
            "updated_at": None,
            "items": {},
        }

    cache = read_json_file(path)
    items = cache.get("items")
    if not isinstance(items, dict):
        cache["items"] = {}
    cache["cache_version"] = version
    cache["month"] = month
    return cache


def canonical_json(data: Any) -> str:
    return json.dumps(data, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def stable_hash(data: Any) -> str:
    return sha256_text(canonical_json(data))


def comment_text_hash(comment: JsonDict) -> str:
    text = comment.get("plain_text")
    if not isinstance(text, str):
        text = ""
    return sha256_text(text)


def make_client(*, dry_run: bool) -> tuple[Any | None, str | None]:
    if load_dotenv is not None:
        load_dotenv()

    if dry_run:
        return None, None

    api_key = os.getenv("LLM_API_KEY")
    model = os.getenv("LLM_MODEL")
    base_url = os.getenv("LLM_BASE_URL") or None

    if not api_key:
        raise AnalyzeError("LLM_API_KEY is required unless --dry-run is used.")
    if not model:
        raise AnalyzeError("LLM_MODEL is required unless --dry-run is used.")
    if OpenAI is None:
        raise AnalyzeError("Missing required package: openai.")

    if base_url:
        return OpenAI(api_key=api_key, base_url=base_url), model
    return OpenAI(api_key=api_key), model


def configured_llm_base_url(client: Any) -> str:
    base_url = getattr(client, "base_url", None)
    return str(base_url).rstrip("/") if base_url else "OpenAI default"


def exception_detail(exc: Exception) -> str:
    """Format provider errors with useful, non-secret diagnostics."""
    parts = [f"{type(exc).__name__}: {exc}"]
    status_code = getattr(exc, "status_code", None)
    if status_code is not None:
        parts.append(f"HTTP status: {status_code}")

    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    if headers:
        request_id = headers.get("x-request-id") or headers.get("request-id")
        if request_id:
            parts.append(f"request ID: {request_id}")
    return "; ".join(parts)


def response_format_is_unsupported(exc: Exception) -> bool:
    """Allow the compatibility fallback only for an unsupported JSON-mode option."""
    status_code = getattr(exc, "status_code", None)
    message = str(exc).lower()
    return status_code in {400, 422} and (
        "response_format" in message
        or "json_object" in message
        or "json mode" in message
    )


def preview_text(text: str, *, max_chars: int = 240) -> str:
    compact = " ".join(text.strip().split())
    if len(compact) <= max_chars:
        return compact
    return compact[: max_chars - 3] + "..."


def strip_markdown_json_fence(text: str) -> str:
    stripped = text.strip()
    fence_match = re.fullmatch(
        r"```(?:json|JSON)?\s*(.*?)\s*```",
        stripped,
        flags=re.DOTALL,
    )
    if fence_match:
        return fence_match.group(1).strip()
    return stripped


def largest_brace_json_candidate(text: str) -> str | None:
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    return text[start : end + 1]


def parse_llm_json(text: str) -> JsonDict:
    """Parse JSON from an LLM response with common formatting fallbacks."""
    parse_errors: list[str] = []

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        parse_errors.append(f"raw JSON parse failed: {exc}")
    else:
        if isinstance(parsed, dict):
            return parsed
        raise ValueError(
            "LLM response JSON must be an object. "
            f"Preview: {preview_text(text)}"
        )

    unfenced = strip_markdown_json_fence(text)
    if unfenced != text.strip():
        try:
            parsed = json.loads(unfenced)
        except json.JSONDecodeError as exc:
            parse_errors.append(f"markdown-fence JSON parse failed: {exc}")
        else:
            if isinstance(parsed, dict):
                return parsed
            raise ValueError(
                "LLM response JSON inside markdown fence must be an object. "
                f"Preview: {preview_text(text)}"
            )

    candidate = largest_brace_json_candidate(unfenced)
    if candidate is not None:
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError as exc:
            parse_errors.append(f"largest-brace JSON parse failed: {exc}")
        else:
            if isinstance(parsed, dict):
                return parsed
            raise ValueError(
                "LLM response JSON object candidate was not an object. "
                f"Preview: {preview_text(text)}"
            )

    detail = "; ".join(parse_errors[-2:]) if parse_errors else "no JSON object found"
    raise ValueError(
        "Could not parse LLM response as JSON "
        f"({detail}). Preview: {preview_text(text)}"
    )


def chat_json(
    *,
    client: Any,
    model: str,
    system_prompt: str,
    user_prompt: str,
) -> tuple[JsonDict, str]:
    kwargs = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.1,
    }

    try:
        response = client.chat.completions.create(
            **kwargs,
            response_format={"type": "json_object"},
        )
    except Exception as exc:
        if not response_format_is_unsupported(exc):
            raise
        print(
            "LLM warning: provider does not accept response_format=json_object; "
            "retrying without JSON mode.",
            file=sys.stderr,
        )
        response = client.chat.completions.create(**kwargs)

    content = response.choices[0].message.content
    if not isinstance(content, str) or not content.strip():
        raise AnalyzeError("LLM returned an empty response.")

    try:
        parsed = parse_llm_json(content)
    except ValueError as exc:
        raise LLMJsonParseError(str(exc), raw_content=content) from exc
    return parsed, content


def as_string_or_none(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    return str(value)


def as_string(value: Any, default: str = "") -> str:
    result = as_string_or_none(value)
    return result if result is not None else default


def as_bool(value: Any) -> bool:
    return bool(value) if isinstance(value, bool) else False


def as_string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def as_number_or_none(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        cleaned = value.replace(",", "").strip()
        if not cleaned:
            return None
        try:
            return float(cleaned)
        except ValueError:
            return None
    return None


def normalize_remote_status(value: Any) -> str:
    status = as_string(value, "unknown").lower()
    return status if status in VALID_REMOTE_STATUSES else "unknown"


def is_relevant_engineering_role_title(title: str) -> bool:
    lowered = title.strip().lower()
    if not lowered or lowered == "unknown role":
        return True

    excluded = (
        "product manager",
        "designer",
        "recruiter",
        "marketing",
        "sales",
        "customer success",
        "content specialist",
        "seo specialist",
        "developer advocate",
        "devrel",
    )
    if has_any(lowered, excluded):
        return False

    engineering_patterns = (
        r"\bswe\b",
        r"\bsoftware engineer\b",
        r"\bbackend engineer\b",
        r"\bback-end engineer\b",
        r"\bfull[\s-]?stack engineer\b",
        r"\bfullstack engineer\b",
        r"\binfrastructure\b",
        r"\bplatform engineer\b",
        r"\binfrastructure engineer\b",
        r"\b(?:sre|site reliability)\b",
        r"\bsecurity\b",
        r"\bsecurity engineer\b",
        r"\bai engineer\b",
        r"\bml engineer\b",
        r"\bdata engineer\b",
        r"\b(?:staff|principal|founding)\s+engineer\b",
        r"\btech lead\b",
        r"\btechnical lead\b",
    )
    if any(re.search(pattern, lowered) for pattern in engineering_patterns):
        return True

    return "engineer" in lowered and not has_any(lowered, excluded)


def normalize_seniority_for_title(title: str, seniority: Any) -> list[str]:
    values = [item.lower() for item in as_string_list(seniority)]
    title_lower = title.lower()
    if has_regex(title_lower, r"\bjunior\s+(?:to|and|\/|-)\s+senior\b"):
        for item in ("junior", "senior", "mixed"):
            if item not in values:
                values.append(item)
    return values


def normalize_extraction_result(result: Any) -> JsonDict:
    data = result if isinstance(result, dict) else {}
    roles = data.get("roles")
    if not isinstance(roles, list):
        roles = []

    normalized_roles: list[JsonDict] = []
    for role in roles:
        if not isinstance(role, dict):
            continue
        title = as_string(role.get("title"), "Unknown role")
        if not is_relevant_engineering_role_title(title):
            continue
        normalized_roles.append(
            {
                "title": title,
                "seniority": normalize_seniority_for_title(title, role.get("seniority")),
                "employment_type": as_string_or_none(role.get("employment_type")),
                "location_raw": as_string_or_none(role.get("location_raw")),
                "remote_status": normalize_remote_status(role.get("remote_status")),
                "remote_region": as_string_or_none(role.get("remote_region")),
                "salary_raw": as_string_or_none(role.get("salary_raw")),
                "salary_min": as_number_or_none(role.get("salary_min")),
                "salary_max": as_number_or_none(role.get("salary_max")),
                "currency": as_string_or_none(role.get("currency")),
                "equity": as_string_or_none(role.get("equity")),
                "tech_stack": as_string_list(role.get("tech_stack")),
                "requirements": as_string_list(role.get("requirements")),
                "nice_to_haves": as_string_list(role.get("nice_to_haves")),
                "apply_email": as_string_or_none(role.get("apply_email")),
                "apply_url": as_string_or_none(role.get("apply_url")),
                "evidence_quotes": as_string_list(role.get("evidence_quotes")),
            }
        )

    return {
        "is_job_post": as_bool(data.get("is_job_post")),
        "company_name": as_string_or_none(data.get("company_name")),
        "company_url": as_string_or_none(data.get("company_url")),
        "company_description": as_string_or_none(data.get("company_description")),
        "roles": normalized_roles,
    }


def normalize_extraction(data: Any) -> JsonDict:
    return normalize_extraction_result(data)


def extraction_system_prompt() -> str:
    return (
        "You extract structured job data from Hacker News Who is Hiring comments. "
        "Use only information explicitly present in the comment. Do not guess salary, "
        "location, remote status, tech stack, visa, or apply URL. Preserve exact "
        "evidence quotes from the post. Return valid JSON only."
    )


def extraction_user_prompt(comment: JsonDict) -> str:
    return (
        "Extract job data from this top-level Hacker News comment.\n\n"
        "Return this JSON object shape exactly:\n"
        "{\n"
        '  "is_job_post": boolean,\n'
        '  "company_name": string|null,\n'
        '  "company_url": string|null,\n'
        '  "company_description": string|null,\n'
        '  "roles": [\n'
        "    {\n"
        '      "title": string,\n'
        '      "seniority": string[],\n'
        '      "employment_type": string|null,\n'
        '      "location_raw": string|null,\n'
        '      "remote_status": "remote"|"hybrid"|"onsite"|"unknown",\n'
        '      "remote_region": string|null,\n'
        '      "salary_raw": string|null,\n'
        '      "salary_min": number|null,\n'
        '      "salary_max": number|null,\n'
        '      "currency": string|null,\n'
        '      "equity": string|null,\n'
        '      "tech_stack": string[],\n'
        '      "requirements": string[],\n'
        '      "nice_to_haves": string[],\n'
        '      "apply_email": string|null,\n'
        '      "apply_url": string|null,\n'
        '      "evidence_quotes": string[]\n'
        "    }\n"
        "  ]\n"
        "}\n\n"
        "Rules:\n"
        "- Use only information explicitly present in the comment.\n"
        "- Do not guess salary, location, remote status, tech stack, visa, or apply URL.\n"
        "- A single comment may contain multiple roles.\n"
        "- Extract only roles that are plausibly software engineering, infrastructure engineering, AI engineering, data/platform engineering, SRE, security engineering, or technical leadership roles.\n"
        "- Do not extract Product, Design, Marketing, Recruiting, Sales, Customer Success, Content, SEO, or DevRel roles unless the candidate profile explicitly targets them.\n"
        '- For role ranges like "Junior to Senior Fullstack Engineer", preserve the title exactly and set seniority to ["junior", "senior", "mixed"].\n'
        "- Do not reject mixed seniority at extraction time.\n"
        "- Preserve exact evidence quotes.\n"
        "- Return valid JSON only.\n\n"
        f"Comment id: {comment.get('id')}\n"
        f"HN URL: {comment.get('hn_url')}\n\n"
        "Comment text:\n"
        f"{as_string(comment.get('plain_text'))}"
    )


def non_empty_lines(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if line.strip()]


def infer_remote_status(text: str) -> str:
    lowered = text.lower()
    if "hybrid" in lowered:
        return "hybrid"
    if "remote" in lowered:
        return "remote"
    if "onsite" in lowered or "on-site" in lowered or "in office" in lowered:
        return "onsite"
    return "unknown"


def extract_salary_line(text: str) -> str | None:
    for line in non_empty_lines(text):
        lowered = line.lower()
        if "$" in line or "salary" in lowered or "compensation" in lowered or "pay" in lowered:
            return line[:300]
    return None


def infer_company_name(text: str) -> str | None:
    for line in non_empty_lines(text):
        cleaned = line.strip(" -|")
        if cleaned:
            return cleaned[:120]
    return None


def infer_role_title(text: str) -> str:
    role_words = (
        "engineer",
        "developer",
        "architect",
        "founding",
        "lead",
        "staff",
        "principal",
        "backend",
        "platform",
        "ai",
    )
    for line in non_empty_lines(text):
        lowered = line.lower()
        if any(word in lowered for word in role_words):
            return line[:160]
    return "Unknown role"


def infer_tech_stack(text: str) -> list[str]:
    patterns = {
        "Python": r"\bpython\b",
        "Java": r"\bjava\b",
        "JavaScript": r"\bjavascript\b",
        "TypeScript": r"\btypescript\b",
        "Node.js": r"\bnode(?:\.js|js)?\b",
        "React": r"\breact\b",
        "Go": r"\bgolang\b|\bgo\b",
        "Rust": r"\brust\b",
        "Ruby": r"\bruby\b",
        "Rails": r"\brails\b",
        "Postgres": r"\bpostgres\b|\bpostgresql\b",
        "MySQL": r"\bmysql\b",
        "MongoDB": r"\bmongodb\b",
        "Redis": r"\bredis\b",
        "AWS": r"\baws\b",
        "GCP": r"\bgcp\b",
        "Azure": r"\bazure\b",
        "Kubernetes": r"\bkubernetes\b|\bk8s\b",
        "Docker": r"\bdocker\b",
        "LLM": r"\bllm\b|\bllms\b",
        "AI": r"\bai\b",
        "Machine Learning": r"\bmachine learning\b|\bml\b",
    }
    return [
        name
        for name, pattern in patterns.items()
        if re.search(pattern, text, flags=re.IGNORECASE)
    ]


def dry_run_extract(comment: JsonDict) -> JsonDict:
    text = as_string(comment.get("plain_text"))
    lowered = text.lower()
    salary_like = re.search(r"\$\s?\d|salary|compensation|\b\d+k\b", lowered) is not None
    is_job_post = (
        "hiring" in lowered
        or "engineer" in lowered
        or "remote" in lowered
        or "full-time" in lowered
        or "full time" in lowered
        or salary_like
    )
    if not is_job_post:
        return normalize_extraction({"is_job_post": False, "roles": []})

    evidence_quote = text.strip()[:300]
    role = {
        "title": "Unknown role",
        "seniority": [],
        "employment_type": None,
        "location_raw": None,
        "remote_status": infer_remote_status(text),
        "remote_region": None,
        "salary_raw": extract_salary_line(text),
        "salary_min": None,
        "salary_max": None,
        "currency": "USD" if "$" in text else None,
        "equity": None,
        "tech_stack": infer_tech_stack(text),
        "requirements": [],
        "nice_to_haves": [],
        "apply_email": None,
        "apply_url": None,
        "evidence_quotes": [evidence_quote] if evidence_quote else [],
    }
    return normalize_extraction(
        {
            "is_job_post": True,
            "company_name": infer_company_name(text),
            "company_url": None,
            "company_description": None,
            "roles": [role],
        }
    )


def cache_entry_reusable(
    entry: Any,
    *,
    expected_hash: str,
    allow_dry_run: bool,
) -> bool:
    if not isinstance(entry, dict):
        return False
    if entry.get("text_hash") != expected_hash:
        return False
    if not isinstance(entry.get("extraction"), dict):
        return False
    return allow_dry_run or entry.get("source") == "llm"


def extract_comments(
    *,
    comments: list[JsonDict],
    cache: JsonDict,
    client: Any | None,
    model: str | None,
    dry_run: bool,
    force: bool,
) -> list[JsonDict]:
    items = cache.setdefault("items", {})
    if not isinstance(items, dict):
        items = {}
        cache["items"] = items

    results: list[JsonDict] = []
    progress = tqdm(comments, desc="Extracting", unit="comment")  # type: ignore[misc]
    for comment in progress:
        comment_id = comment.get("id")
        cache_key = str(comment_id)
        text_hash = comment_text_hash(comment)
        cached = items.get(cache_key)

        if not force and cache_entry_reusable(
            cached,
            expected_hash=text_hash,
            allow_dry_run=dry_run,
        ):
            results.append(cached)
            continue

        created_at = utc_now_iso()
        raw_content: str | None = None
        parse_error: str | None = None

        if dry_run:
            extraction = dry_run_extract(comment)
            source = "dry_run_heuristic"
        else:
            try:
                parsed, raw_content = chat_json(
                    client=client,
                    model=str(model),
                    system_prompt=extraction_system_prompt(),
                    user_prompt=extraction_user_prompt(comment),
                )
                extraction = normalize_extraction(parsed)
                source = "llm"
            except LLMJsonParseError as exc:
                raise AnalyzeError(
                    f"LLM extraction returned invalid JSON for comment {comment_id}. "
                    f"Model: {model}. {exc}"
                ) from exc
            except Exception as exc:
                raise AnalyzeError(
                    f"LLM extraction request failed for comment {comment_id}. "
                    f"Model: {model}. Base URL: {configured_llm_base_url(client)}. "
                    f"{exception_detail(exc)}\n"
                    "Check that LLM_BASE_URL and LLM_MODEL are valid for this API key. "
                    "After correcting them, rerun with --clear-analysis to discard "
                    "the failed month's generated artifacts."
                ) from exc

        entry: JsonDict = {
            "comment_id": comment_id,
            "text_hash": text_hash,
            "source": source,
            "model": model,
            "created_at": created_at,
            "raw_content": raw_content,
            "parse_error": parse_error,
            "extraction": extraction,
        }
        items[cache_key] = entry
        results.append(entry)

    cache["updated_at"] = utc_now_iso()
    return results


def flatten_jobs(
    *,
    extraction_entries: list[JsonDict],
    comments_by_id: dict[Any, JsonDict],
) -> list[JsonDict]:
    jobs: list[JsonDict] = []
    for entry in extraction_entries:
        extraction = entry.get("extraction")
        if not isinstance(extraction, dict) or not extraction.get("is_job_post"):
            continue

        roles = extraction.get("roles")
        if not isinstance(roles, list):
            continue

        comment_id = entry.get("comment_id")
        comment = comments_by_id.get(comment_id, {})
        for role_index, role in enumerate(roles):
            if not isinstance(role, dict):
                continue

            job: JsonDict = {
                "job_id": f"{comment_id}:{role_index}",
                "comment_id": comment_id,
                "comment_text_hash": entry.get("text_hash"),
                "role_index": role_index,
                "company_name": extraction.get("company_name"),
                "company_url": extraction.get("company_url"),
                "company_description": extraction.get("company_description"),
                "role": role,
                "source_comment": {
                    "id": comment.get("id"),
                    "by": comment.get("by"),
                    "time": comment.get("time"),
                    "time_iso": comment.get("time_iso"),
                    "hn_url": comment.get("hn_url"),
                    "plain_text": comment.get("plain_text"),
                    "source_thread_id": comment.get("source_thread_id"),
                    "source_thread_title": comment.get("source_thread_title"),
                    "position_index": comment.get("position_index"),
                },
            }
            job["job_hash"] = stable_hash(job_for_hash(job))
            jobs.append(job)
    return jobs


def job_for_hash(job: JsonDict) -> JsonDict:
    return {
        "comment_id": job.get("comment_id"),
        "comment_text_hash": job.get("comment_text_hash"),
        "role_index": job.get("role_index"),
        "company_name": job.get("company_name"),
        "company_url": job.get("company_url"),
        "company_description": job.get("company_description"),
        "role": job.get("role"),
    }


def combined_hard_filter_text(job: JsonDict) -> str:
    """Lowercased text used only for clear deterministic hard rejections.

    Ambiguous cases should pass through to LLM ranking, where nuance and missing
    context can be handled without prematurely discarding a possibly good job.
    """
    role = job.get("role") if isinstance(job.get("role"), dict) else {}
    parts = [
        role.get("title"),
        job.get("company_name"),
        role.get("location_raw"),
        role.get("remote_status"),
        role.get("remote_region"),
        role.get("employment_type"),
        role.get("salary_raw"),
        " ".join(as_string_list(role.get("tech_stack"))),
        " ".join(as_string_list(role.get("requirements"))),
        " ".join(as_string_list(role.get("evidence_quotes"))),
    ]
    return " ".join(as_string(part) for part in parts if part is not None).lower()


def role_text(job: JsonDict) -> str:
    role = job.get("role") if isinstance(job.get("role"), dict) else {}
    parts = [
        job.get("company_name"),
        job.get("company_description"),
        role.get("title"),
        " ".join(as_string_list(role.get("seniority"))),
        role.get("employment_type"),
        role.get("location_raw"),
        role.get("remote_status"),
        role.get("remote_region"),
        role.get("salary_raw"),
        role.get("currency"),
        role.get("equity"),
        " ".join(as_string_list(role.get("tech_stack"))),
        " ".join(as_string_list(role.get("requirements"))),
        " ".join(as_string_list(role.get("nice_to_haves"))),
    ]
    return " ".join(as_string(part) for part in parts if part is not None).lower()


def heuristic_job_text(job: JsonDict) -> str:
    role = job.get("role") if isinstance(job.get("role"), dict) else {}
    parts = [
        job.get("company_name"),
        role.get("title"),
        job.get("company_description"),
        role.get("location_raw"),
        role.get("remote_status"),
        role.get("remote_region"),
        role.get("salary_raw"),
        " ".join(as_string_list(role.get("tech_stack"))),
        " ".join(as_string_list(role.get("requirements"))),
        " ".join(as_string_list(role.get("nice_to_haves"))),
        " ".join(as_string_list(role.get("evidence_quotes"))),
    ]
    return " ".join(as_string(part) for part in parts if part is not None).lower()


def title_text(job: JsonDict) -> str:
    role = job.get("role") if isinstance(job.get("role"), dict) else {}
    return as_string(role.get("title")).lower()


def profile_hard_filters(profile: JsonDict) -> JsonDict:
    filters = profile.get("hard_filters")
    return filters if isinstance(filters, dict) else {}


def number_word_to_int(value: str) -> int | None:
    words = {
        "one": 1,
        "two": 2,
        "three": 3,
        "four": 4,
        "five": 5,
    }
    if value.isdigit():
        return int(value)
    return words.get(value.lower())


def detect_hybrid_days(text: str) -> int | None:
    lowered = text.lower()
    if has_regex(lowered, r"\bone\s+week\s+per\s+month\s+(?:onsite|on-site|in\s+office)\b"):
        return 2

    patterns = [
        r"\b(\d+|one|two|three|four|five)\s*x\s*/?\s*(?:week|wk)\b",
        r"\b(\d+|one|two|three|four|five)\s*(?:days?|d)\s*/\s*(?:week|wk)\b",
        r"\b(\d+|one|two|three|four|five)\s*(?:days?|d)\s*(?:per|a)\s*(?:week|wk)\b",
        r"\b(\d+|one|two|three|four|five)\s*(?:days?|d)\s*(?:in\s*)?(?:office|onsite|on-site)\b",
    ]
    for pattern in patterns:
        match = re.search(pattern, lowered, flags=re.IGNORECASE)
        if match:
            return number_word_to_int(match.group(1))
    return None


def hybrid_days_per_week(text: str) -> int | None:
    return detect_hybrid_days(text)


def offers_remote_option(text: str) -> bool:
    lowered = text.lower()
    return (
        has_any(
            lowered,
            (
                "or remote",
                "remote or onsite",
                "remote or on-site",
                "onsite or remote",
                "on-site or remote",
                "remote / onsite",
                "remote / on-site",
                "remote and onsite",
                "remote and on-site",
                "fully remote",
                "remote worldwide",
                "remote anywhere",
                "remote global",
                "remote (global)",
                "remote (worldwide)",
            ),
        )
        or has_regex(lowered, r"\bonsite\b.{0,80}\bor\s+remote\b")
        or has_regex(lowered, r"\bon-site\b.{0,80}\bor\s+remote\b")
    )


def is_clearly_onsite_only(text: str) -> bool:
    lowered = text.lower()
    if offers_remote_option(lowered):
        return False
    return (
        has_any(
            lowered,
            (
                "| onsite |",
                "(onsite)",
                "onsite only",
                "on-site only",
                "on-site daily",
                "onsite daily",
                "in-person work",
                "in person work",
                "must be onsite",
                "onsite in ",
                "on-site in ",
            ),
        )
        or has_regex(lowered, r"\|\s*on-?site\b")
        or has_regex(lowered, r"\blocation:\s*.{0,120}\bon-?site\b")
        or has_regex(lowered, r"\bbased in\s*.{0,120}\bon-?site\b")
    )


def has_any(text: str, needles: tuple[str, ...]) -> bool:
    return any(needle in text for needle in needles)


def has_regex(text: str, pattern: str) -> bool:
    return re.search(pattern, text, flags=re.IGNORECASE) is not None


def profile_skills_text(profile: JsonDict) -> str:
    skills = profile.get("skills")
    if not isinstance(skills, dict):
        return ""
    parts: list[str] = []
    for value in skills.values():
        parts.extend(as_string_list(value))
    return " ".join(parts).lower()


def profile_avoid_text(profile: JsonDict) -> str:
    parts: list[str] = []
    avoid_roles = profile.get("avoid_roles")
    if isinstance(avoid_roles, list):
        parts.extend(as_string_list(avoid_roles))
    skills = profile.get("skills")
    if isinstance(skills, dict):
        parts.extend(as_string_list(skills.get("avoid_or_not_primary")))
    domains = profile.get("domains")
    if isinstance(domains, dict):
        parts.extend(as_string_list(domains.get("avoid")))
    return " ".join(parts).lower()


def candidate_profile(profile: JsonDict) -> JsonDict:
    candidate = profile.get("candidate")
    return candidate if isinstance(candidate, dict) else {}


def parse_utc_offset(value: str) -> float | None:
    match = re.search(
        r"([+-])\s*(\d{1,2})(?::(\d{1,2})|\.(\d+))?",
        value.strip(),
        flags=re.IGNORECASE,
    )
    if not match:
        return None
    sign = -1.0 if match.group(1) == "-" else 1.0
    hours = float(match.group(2))
    minutes = match.group(3)
    decimal = match.group(4)
    if minutes is not None:
        hours += float(minutes) / 60.0
    elif decimal is not None:
        hours += float(f"0.{decimal}")
    return sign * hours


def candidate_timezone_offset(profile: JsonDict) -> float | None:
    timezone = as_string(candidate_profile(profile).get("timezone")).strip()
    if not timezone:
        return None
    lowered = timezone.lower()
    if lowered == "asia/kolkata":
        return 5.5
    if lowered.startswith(("utc", "gmt")):
        return parse_utc_offset(lowered.removeprefix("utc").removeprefix("gmt"))
    return None


def detect_timezone_window(text: str) -> tuple[float, float] | None:
    lowered = text.lower()
    if has_regex(lowered, r"\b(?:eu\s+)?cet\b.{0,16}(?:\+/-|\+-)\s*2\b"):
        return (-1.0, 3.0)

    utc_window = re.search(
        r"\b(?:utc|gmt)\s*([+-]\s*\d{1,2}(?::\d{1,2}|\.\d+)?)\s*(?:-|to)\s*"
        r"(?:(?:utc|gmt)\s*)?([+-]\s*\d{1,2}(?::\d{1,2}|\.\d+)?)\b",
        lowered,
        flags=re.IGNORECASE,
    )
    if not utc_window:
        return None

    start = parse_utc_offset(utc_window.group(1))
    end = parse_utc_offset(utc_window.group(2))
    if start is None or end is None:
        return None
    return (min(start, end), max(start, end))


def timezone_compatible(text: str, profile: JsonDict) -> tuple[bool, str]:
    window = detect_timezone_window(text)
    if window is None:
        return True, ""
    candidate_offset = candidate_timezone_offset(profile)
    if candidate_offset is None:
        return True, "Candidate timezone could not be parsed."
    lower, upper = window
    if lower - 1.0 <= candidate_offset <= upper + 1.0:
        return True, ""
    return (
        False,
        f"Timezone window UTC{lower:+g} to UTC{upper:+g} excludes candidate UTC{candidate_offset:+g}.",
    )


def first_comment_line(text: str) -> str:
    lines = non_empty_lines(text)
    return lines[0].lower() if lines else ""


def is_mixed_seniority_text(text: str) -> bool:
    lowered = text.lower()
    return has_any(
        lowered,
        (
            "junior to senior",
            "junior and senior",
            "junior & senior",
            "junior/senior",
            "junior through senior",
            "mid to senior",
            "multiple levels",
            "senior and junior",
            "senior & junior",
            "senior and junior roles",
        ),
    )


def is_junior_or_intern_post(text: str) -> tuple[bool, str]:
    lowered = text.lower()
    first_line = first_comment_line(lowered)
    if is_mixed_seniority_text(first_line) or is_mixed_seniority_text(lowered):
        return False, "Mixed seniority posting; verify senior-level opening exists."

    strong_title_text = f"{first_line} {lowered[:240]}"
    if has_regex(strong_title_text, r"\b(?:intern|internship)\b"):
        return True, "Internship role."
    if has_regex(
        strong_title_text,
        r"\b(?:junior|junior engineer|junior software engineer|entry level|entry-level|"
        r"new grad|graduate engineer)\b",
    ):
        return True, "Junior or entry-level role."
    return False, ""


def is_worldwide_remote_text(text: str) -> bool:
    lowered = text.lower()
    return has_any(
        lowered,
        (
            "remote worldwide",
            "worldwide remote",
            "remote anywhere",
            "anywhere remote",
            "remote from anywhere",
            "any time zone",
            "any timezone",
            "global remote",
            "remote global",
            "remote (global)",
            "remote (worldwide)",
            "remote (anywhere)",
        ),
    ) or has_regex(lowered, r"\b(?:worldwide|anywhere|global)\b.{0,40}\bremote\b")


def has_software_role_signal(text: str) -> bool:
    return has_regex(
        text,
        r"\b(?:software|backend|back-end|full stack|full-stack|fullstack|platform|"
        r"infrastructure|api|devops|sre|data engineer|machine learning|ml engineer|"
        r"ai engineer|engineer|developer|architect)\b",
    )


def has_backend_or_platform_signal(text: str) -> bool:
    return has_regex(
        text,
        r"\b(?:backend|back-end|full stack|full-stack|fullstack|platform|"
        r"infrastructure|api|server-side|distributed|devops|sre|data|retrieval)\b",
    )


def is_mobile_engineering_role(text: str) -> bool:
    lowered = text.lower()
    if has_regex(
        lowered,
        r"\b(?:mobile\s+(?:engineer|developer|app|applications?)|"
        r"ios\s+engineer|android\s+engineer|react native|flutter|"
        r"cross-platform mobile)\b",
    ):
        return True
    if has_regex(lowered, r"\bsenior\s+react native\s+developer\b"):
        return True
    if has_regex(lowered, r"\bswift\b") and has_regex(lowered, r"\b(?:ios|mobile|app|applications?)\b"):
        return True
    if has_regex(lowered, r"\bkotlin\b") and has_regex(lowered, r"\b(?:android|mobile|app|applications?)\b"):
        return True
    return False


def comment_job_signal(text: str) -> bool:
    return has_regex(
        text,
        r"\b(?:hiring|engineer|developer|salary|compensation|full[- ]time|remote|"
        r"apply|we are looking|we're looking|role|roles)\b",
    )


def text_has_salary_above_minimum(text: str, profile: JsonDict) -> bool:
    filters = profile_hard_filters(profile)
    threshold = as_number_or_none(filters.get("minimum_salary_if_disclosed"))
    if threshold is None:
        return False

    values: list[float] = []
    for match in re.finditer(r"\$\s?(\d{2,3})(?:\s?k|\s*,000)?", text, flags=re.IGNORECASE):
        value = float(match.group(1))
        if value < 1000:
            value *= 1000
        values.append(value)
    return bool(values) and max(values) >= threshold


def candidate_can_work(profile: JsonDict, region: str) -> bool:
    candidate = candidate_profile(profile)
    key = f"can_work_in_{region}"
    return bool(candidate.get(key))


EU_COUNTRIES = {
    "austria",
    "belgium",
    "bulgaria",
    "croatia",
    "cyprus",
    "czech republic",
    "czechia",
    "denmark",
    "estonia",
    "finland",
    "france",
    "germany",
    "greece",
    "hungary",
    "ireland",
    "italy",
    "latvia",
    "lithuania",
    "luxembourg",
    "malta",
    "netherlands",
    "poland",
    "portugal",
    "romania",
    "slovakia",
    "slovenia",
    "spain",
    "sweden",
}

LATAM_COUNTRIES = {
    "argentina",
    "bolivia",
    "brazil",
    "chile",
    "colombia",
    "costa rica",
    "cuba",
    "dominican republic",
    "ecuador",
    "el salvador",
    "guatemala",
    "honduras",
    "mexico",
    "nicaragua",
    "panama",
    "paraguay",
    "peru",
    "puerto rico",
    "uruguay",
    "venezuela",
}

APAC_COUNTRIES = {
    "australia",
    "bangladesh",
    "china",
    "hong kong",
    "india",
    "indonesia",
    "japan",
    "malaysia",
    "new zealand",
    "pakistan",
    "philippines",
    "singapore",
    "south korea",
    "sri lanka",
    "taiwan",
    "thailand",
    "vietnam",
}

NORTH_AMERICA_COUNTRIES = {
    "us",
    "canada",
    "mexico",
}

EMEA_COUNTRIES = EU_COUNTRIES | {
    "uk",
    "united kingdom",
    "israel",
    "turkey",
    "united arab emirates",
    "uae",
    "saudi arabia",
    "qatar",
    "south africa",
    "egypt",
    "kenya",
    "nigeria",
    "morocco",
}


def normalized_region_phrase(phrase: str) -> str:
    return re.sub(r"\s+", " ", phrase.strip().lower())


def candidate_country(profile: JsonDict) -> str:
    candidate = candidate_profile(profile)
    country = as_string(candidate.get("country")).lower()
    if not country:
        country = as_string(candidate.get("location")).lower()
    if "india" in country:
        return "india"
    if "united states" in country or re.search(r"\busa\b|\bus\b|\bu\.s\.", country):
        return "us"
    if "canada" in country:
        return "canada"
    if "uk" in country or "united kingdom" in country:
        return "uk"
    return normalized_region_phrase(country)


def candidate_region(profile: JsonDict) -> str:
    candidate = candidate_profile(profile)
    return normalized_region_phrase(as_string(candidate.get("region")))


def add_region(regions: list[str], region: str) -> None:
    if region and region not in regions:
        regions.append(region)


def detect_geo_restrictions(text: str) -> JsonDict:
    lowered = normalized_region_phrase(text)
    regions: list[str] = []
    reasons: list[str] = []

    def detect(region: str, pattern: str, reason: str) -> None:
        if has_regex(lowered, pattern):
            add_region(regions, region)
            reasons.append(reason)

    if is_worldwide_remote_text(lowered):
        return {"is_region_locked": False, "regions": [], "reason": None}

    detect(
        "US",
        r"\b(?:remote\s*\(?u\.?s\.?\)?|remote\s*\(?usa\)?|(?:u\.?s\.?|usa|united states)\s*\(?remote\)?|"
        r"us\s+only|u\.s\.\s+only|"
        r"usa\s+only|united states\s+only|must be living inside united states|"
        r"must be based in (?:the )?(?:us|u\.s\.|usa|united states))(?=$|[^a-z0-9])",
        "US-only remote/location restriction.",
    )
    detect(
        "EU",
        r"\b(?:remote\s*\(?eu\)?|remote\s*\(?europe\)?|(?:eu|europe)\s*\(?remote\)?|"
        r"europe\s+only|eu\s+only|"
        r"must be based in (?:the )?(?:eu|europe))(?=$|[^a-z0-9])",
        "EU/Europe-only remote/location restriction.",
    )
    detect(
        "UK",
        r"\b(?:remote\s*\(?uk\)?|uk\s+only|united kingdom\s+only|"
        r"must be based in (?:the )?(?:uk|united kingdom))(?=$|[^a-z0-9])",
        "UK-only remote/location restriction.",
    )
    detect(
        "Canada",
        r"\b(?:remote\s*\(?canada\)?|canada\s+only|must be based in canada)(?=$|[^a-z0-9])",
        "Canada-only remote/location restriction.",
    )
    detect(
        "Japan",
        r"\b(?:remote in japan|remote japan|japan\s*\(?remote\)?|must be based in japan|relocation to japan required)(?=$|[^a-z0-9])",
        "Japan-only remote/location restriction.",
    )
    detect(
        "Italy",
        r"\b(?:remote\s*\(?italy\)?|italy\s+only|must be based in italy)(?=$|[^a-z0-9])",
        "Italy-only remote/location restriction.",
    )
    detect(
        "Poland",
        r"\b(?:remote\s*\(?poland\)?|poland\s+only|must be based in poland)(?=$|[^a-z0-9])",
        "Poland-only remote/location restriction.",
    )
    detect(
        "Romania",
        r"\b(?:remote\s*\(?romania\)?|romania\s+only|must be based in romania)(?=$|[^a-z0-9])",
        "Romania-only remote/location restriction.",
    )
    if has_regex(lowered, r"\bmust be based in poland or romania\b"):
        add_region(regions, "Poland")
        add_region(regions, "Romania")
        reasons.append("Poland/Romania-only remote/location restriction.")
    detect(
        "Oregon",
        r"\b(?:oregon\s+usa\s+only|remote okay\.\s*oregon\s+usa\s+only|oregon\s+only)(?=$|[^a-z0-9])",
        "Oregon-only remote/location restriction.",
    )
    detect(
        "LATAM",
        r"\b(?:latam\s+remote|remote\s*\(?latam\)?|remote\s+latin america|latin america\s+remote|latin america\s+only|south america\s+remote|remote\s+south america|south america\s+only)(?=$|[^a-z0-9])",
        "LATAM/Latin America-only remote/location restriction.",
    )
    detect(
        "North America",
        r"\b(?:north america\s+remote|remote\s*\(?north america\)?|north america\s+only)(?=$|[^a-z0-9])",
        "North America-only remote/location restriction.",
    )
    detect(
        "Americas",
        r"\b(?:americas\s+remote|remote\s*\(?americas\)?|americas\s+only)(?=$|[^a-z0-9])",
        "Americas-only remote/location restriction.",
    )
    detect(
        "APAC",
        r"\b(?:apac\s+remote|remote\s*\(?apac\)?|apac\s+only)(?=$|[^a-z0-9])",
        "APAC-only remote/location restriction.",
    )
    detect(
        "EMEA",
        r"\b(?:emea\s+remote|remote\s*\(?emea\)?|emea\s+only)(?=$|[^a-z0-9])",
        "EMEA-only remote/location restriction.",
    )
    if has_regex(lowered, r"\b(?:remote\s*\(?emea/apac\)?|remote\s*\(?apac/emea\)?|emea/apac\s+remote|apac/emea\s+remote)(?=$|[^a-z0-9])"):
        add_region(regions, "EMEA")
        add_region(regions, "APAC")
        reasons.append("EMEA/APAC-only remote/location restriction.")
    detect(
        "India",
        r"\b(?:india\s+remote|remote\s*\(?india\)?|remote in india|india\s+only|must be based in india)(?=$|[^a-z0-9])",
        "India-only remote/location restriction.",
    )
    detect(
        "Singapore",
        r"\b(?:singapore\s+remote|remote\s*\(?singapore\)?|remote in singapore|singapore\s+only|must be based in singapore)(?=$|[^a-z0-9])",
        "Singapore-only remote/location restriction.",
    )
    detect(
        "Philippines",
        r"\b(?:philippines\s+remote|remote\s*\(?philippines\)?|remote in philippines|philippines\s+only|must be based in philippines)(?=$|[^a-z0-9])",
        "Philippines-only remote/location restriction.",
    )
    detect(
        "Australia",
        r"\b(?:australia\s+remote|remote\s*\(?australia\)?|remote in australia|australia\s+only|must be based in australia)(?=$|[^a-z0-9])",
        "Australia-only remote/location restriction.",
    )
    detect(
        "New Zealand",
        r"\b(?:new zealand\s+remote|remote\s*\(?new zealand\)?|remote in new zealand|new zealand\s+only|must be based in new zealand)(?=$|[^a-z0-9])",
        "New Zealand-only remote/location restriction.",
    )

    specific_based = re.search(
        r"\bmust (?:be based|reside|be located|live) in ([a-z][a-z\s/,-]{1,80}?)(?: only)?(?:[.;|,\n]|$)",
        lowered,
    )
    if specific_based:
        phrase = specific_based.group(1).strip(" .,-|")
        for country in (
            "japan",
            "italy",
            "poland",
            "romania",
            "canada",
            "india",
            "singapore",
            "philippines",
            "australia",
            "new zealand",
        ):
            if country in phrase:
                add_region(regions, country.title())
                reasons.append(f"{country.title()}-only remote/location restriction.")
        if "latam" in phrase or "latin america" in phrase or "south america" in phrase:
            add_region(regions, "LATAM")
            reasons.append("LATAM/Latin America-only remote/location restriction.")
        if "north america" in phrase:
            add_region(regions, "North America")
            reasons.append("North America-only remote/location restriction.")
        if "americas" in phrase:
            add_region(regions, "Americas")
            reasons.append("Americas-only remote/location restriction.")
        if "apac" in phrase:
            add_region(regions, "APAC")
            reasons.append("APAC-only remote/location restriction.")
        if "emea" in phrase:
            add_region(regions, "EMEA")
            reasons.append("EMEA-only remote/location restriction.")
        if "united states" in phrase or re.search(r"\b(?:us|u\.s\.|usa)\b", phrase):
            add_region(regions, "US")
            reasons.append("US-only remote/location restriction.")
        if "europe" in phrase or re.search(r"\beu\b", phrase):
            add_region(regions, "EU")
            reasons.append("EU/Europe-only remote/location restriction.")
        if "uk" in phrase or "united kingdom" in phrase:
            add_region(regions, "UK")
            reasons.append("UK-only remote/location restriction.")

    reason = "; ".join(dict.fromkeys(reasons)) if reasons else None
    return {
        "is_region_locked": bool(regions),
        "regions": regions,
        "reason": reason,
    }


def region_is_candidate_compatible(region: str, profile: JsonDict) -> bool:
    country = candidate_country(profile)
    candidate_state = candidate_region(profile)
    normalized = normalized_region_phrase(region)
    if normalized == "us":
        return candidate_can_work(profile, "us") or country == "us"
    if normalized == "eu":
        return candidate_can_work(profile, "eu") or country in EU_COUNTRIES
    if normalized == "uk":
        return candidate_can_work(profile, "uk") or country == "uk"
    if normalized in {"latam", "latin america", "south america"}:
        return country in LATAM_COUNTRIES
    if normalized == "north america":
        return (
            country in NORTH_AMERICA_COUNTRIES
            or candidate_can_work(profile, "us")
            or candidate_can_work(profile, "canada")
        )
    if normalized == "americas":
        return country in NORTH_AMERICA_COUNTRIES or country in LATAM_COUNTRIES
    if normalized == "apac":
        return country in APAC_COUNTRIES
    if normalized == "emea":
        return country in EMEA_COUNTRIES
    if normalized == "oregon":
        return candidate_state == "oregon"
    if normalized in {
        "japan",
        "italy",
        "poland",
        "romania",
        "canada",
        "india",
        "singapore",
        "philippines",
        "australia",
        "new zealand",
    }:
        return country == normalized
    return country == normalized


def region_matches_candidate_location(region: str, profile: JsonDict) -> bool:
    country = candidate_country(profile)
    candidate_state = candidate_region(profile)
    normalized = normalized_region_phrase(region)
    if normalized in {country, candidate_state}:
        return True
    if normalized == "eu":
        return country in EU_COUNTRIES
    if normalized in {"latam", "latin america", "south america"}:
        return country in LATAM_COUNTRIES
    if normalized == "north america":
        return country in NORTH_AMERICA_COUNTRIES
    if normalized == "americas":
        return country in NORTH_AMERICA_COUNTRIES or country in LATAM_COUNTRIES
    if normalized == "apac":
        return country in APAC_COUNTRIES
    if normalized == "emea":
        return country in EMEA_COUNTRIES
    return False


def is_geo_compatible(restrictions: JsonDict, profile: JsonDict) -> tuple[bool, str]:
    if not restrictions.get("is_region_locked"):
        return True, ""

    regions = as_string_list(restrictions.get("regions"))
    compatible_regions = [region for region in regions if region_is_candidate_compatible(region, profile)]
    if compatible_regions:
        candidate = candidate_profile(profile)
        if bool(candidate.get("requires_visa_sponsorship")) and not bool(
            profile_hard_filters(profile).get("allow_country_locked_without_sponsorship")
        ):
            country = candidate_country(profile)
            state = candidate_region(profile)
            exact_candidate_region = any(
                normalized_region_phrase(region) in {country, state}
                or region_matches_candidate_location(region, profile)
                for region in compatible_regions
            )
            if not exact_candidate_region:
                return (
                    False,
                    "Country/region-locked role lacks explicit sponsorship compatibility.",
                )
        return True, ""

    reason = as_string(restrictions.get("reason"), "Region-locked role.")
    return False, reason


def has_candidate_ineligible_region(text: str, profile: JsonDict) -> str | None:
    if is_worldwide_remote_text(text):
        return None
    candidate = candidate_profile(profile)
    ineligible = candidate.get("ineligible_remote_regions")
    if not isinstance(ineligible, list):
        return None
    for item in as_string_list(ineligible):
        phrase = normalized_region_phrase(item)
        if phrase and phrase in text:
            return item
    return None


def has_comment_geo_mismatch(text: str, profile: JsonDict) -> str | None:
    phrase = has_candidate_ineligible_region(text, profile)
    if phrase:
        return f"Ineligible remote region: {phrase}."

    restrictions = detect_geo_restrictions(text)
    compatible, reason = is_geo_compatible(restrictions, profile)
    if not compatible:
        return reason
    return None


def detected_hybrid_days(text: str) -> int | None:
    return detect_hybrid_days(text)


def comment_prefilter_counts(results: list[JsonDict]) -> JsonDict:
    counts = {"total": len(results), "keep": 0, "deprioritize": 0, "reject": 0}
    for result in results:
        prefilter = result.get("prefilter") if isinstance(result.get("prefilter"), dict) else {}
        decision = as_string(prefilter.get("decision"))
        if decision in counts:
            counts[decision] += 1
    return counts


def prefilter_comment(comment: JsonDict, profile: JsonDict) -> JsonDict:
    """Conservative comment-level gate before LLM extraction.

    This rejects only obvious whole-comment mismatches. Ambiguous or mixed-role
    comments should be kept or deprioritized so extraction can preserve nuance.
    """
    text = as_string(comment.get("plain_text")).lower()
    header = first_comment_line(text)
    filters = profile_hard_filters(profile)
    avoid_text = profile_avoid_text(profile)
    skills_text = profile_skills_text(profile)

    reasons: list[str] = []
    signals: list[str] = []
    score = 0
    decision = "keep"

    def reject(reason: str) -> None:
        nonlocal decision
        if decision != "reject":
            decision = "reject"
        reasons.append(reason)

    def deprioritize(reason: str) -> None:
        nonlocal decision
        if decision != "reject":
            decision = "deprioritize"
        reasons.append(reason)

    def signal(label: str, delta: int) -> None:
        nonlocal score
        signals.append(label)
        score += delta

    if not text.strip():
        reject("Empty comment.")
    elif has_any(
        text,
        (
            "when it comes to motivation",
            "thank you for the kind words",
            "thanks for the kind words",
        ),
    ) and not comment_job_signal(text):
        reject("Clearly appears to be a non-job/meta comment.")

    if has_any(text, ("equity only", "equity-only", "unpaid", "no salary", "no cash compensation")):
        reject("Unpaid or equity-only comment.")

    junior_or_intern_post, junior_reason = is_junior_or_intern_post(text)
    if junior_reason == "Mixed seniority posting; verify senior-level opening exists.":
        deprioritize(junior_reason)
    elif junior_or_intern_post:
        if filters.get("reject_internships") and junior_reason == "Internship role.":
            reject("Internship comment.")
        elif filters.get("reject_junior_roles") and junior_reason == "Junior or entry-level role.":
            reject("Junior or entry-level comment.")

    if filters.get("reject_onsite_only") and is_clearly_onsite_only(text):
        reject("Clearly onsite-only comment.")

    allowed_hybrid = as_number_or_none(filters.get("reject_hybrid_more_than_days_per_week"))
    has_hybrid_or_required_onsite = (
        "hybrid" in text
        or (
            not offers_remote_option(text)
            and has_regex(text, r"\b(?:onsite|on-site|in office|in-office)\b")
        )
    )
    if allowed_hybrid is not None and has_hybrid_or_required_onsite:
        days = detected_hybrid_days(text)
        if days is not None and days > allowed_hybrid:
            reject(f"Hybrid requirement appears to be {days} days/week, above allowed limit.")
        elif days is None and "hybrid" in text:
            deprioritize("Hybrid mentioned but office frequency is unclear.")

    geo_reason = has_comment_geo_mismatch(text, profile)
    if filters.get("reject_wrong_geo") and geo_reason:
        reject(f"Region-locked remote role incompatible with candidate profile: {geo_reason}")

    timezone_ok, timezone_reason = timezone_compatible(text, profile)
    if not timezone_ok:
        deprioritize("Timezone window appears incompatible with candidate timezone.")

    non_software_terms = (
        "technical program manager",
        "product manager",
        "designer",
        "recruiter",
        "marketing",
        "sales",
        "customer success",
        "content specialist",
    )
    if has_any(text, non_software_terms):
        if has_software_role_signal(text):
            deprioritize("Mixed role-family comment includes non-software roles.")
        else:
            reject("Obvious non-software role family.")

    avoids_hardware = has_any(avoid_text, ("hardware", "firmware", "embedded", "mechanical"))
    hardware_only = (
        "principal mechanical" in text
        or "kinetic-art studio" in text
        or ("firmware engineer" in text and "embedded systems" in text)
        or ("hardware" in text and ("onsite" in text or "on-site" in text))
        or ("robotics" in text and ("onsite" in text or "on-site" in text))
    )
    if avoids_hardware and hardware_only:
        if has_backend_or_platform_signal(text):
            deprioritize("Hardware/robotics terms present, but software roles may also be present.")
        else:
            reject("Clearly hardware, firmware, mechanical, or onsite robotics focused.")

    mobile_only = is_mobile_engineering_role(text) and not has_backend_or_platform_signal(text)
    frontend_only = has_regex(text, r"\b(?:front[\s-]?end|frontend)\s+engineer\b") and not has_backend_or_platform_signal(text)
    if filters.get("reject_mobile_only") and mobile_only:
        reject("Clearly mobile-only comment.")
    if filters.get("reject_frontend_only") and frontend_only:
        reject("Clearly frontend-only comment.")
    if has_regex(text, r"\bfull[\s-]?stack\b") and "backend" in skills_text:
        deprioritize("Full-stack appears, but candidate primarily prefers backend/platform work.")

    specialized_terms = ("gpu engineer", "cuda", "ptx", "kernel optimization", "compiler engineer", "embedded")
    if has_any(text, specialized_terms):
        if not has_backend_or_platform_signal(text):
            if has_any(avoid_text, ("embedded", "hardware")) or "rust" not in skills_text:
                reject("Narrow low-level specialization appears outside candidate primary skills.")
            else:
                deprioritize("Narrow low-level specialization may be outside primary strengths.")
        else:
            deprioritize("Specialized low-level terms present alongside broader engineering signals.")

    if is_worldwide_remote_text(text):
        signal("Remote worldwide/anywhere/global signal.", 20)
    elif "remote" in text:
        signal("Remote signal.", 10)
    if has_regex(text, r"\b(?:senior|staff|principal|founding|lead)\b"):
        signal("Senior/staff/principal/founding/lead signal.", 20)
    if has_regex(text, r"\b(?:backend|back-end|platform|infrastructure|distributed|api|data|retrieval)\b"):
        signal("Backend/platform/infrastructure/data signal.", 20)
    if has_regex(text, r"\b(?:ai|llm|rag|agents?|devtools?|developer tools|b2b|saas)\b"):
        signal("AI/devtools/B2B/SaaS signal.", 15)
    if text_has_salary_above_minimum(text, profile):
        signal("Salary appears disclosed above configured minimum.", 10)
    if has_regex(text, r"\b(?:python|typescript|sql|postgres|postgresql|aws|gcp|docker)\b"):
        signal("Preferred technology signal.", 10)

    if not has_regex(text, r"\b(?:salary|compensation|\$\s?\d|\d{2,3}\s?k)\b"):
        signal("No salary signal.", -5)
    if len(text.strip()) < 160:
        signal("Vague or very short post.", -15)
        if decision == "keep":
            deprioritize("Vague or very short comment.")
    if "frontend" in text or "front-end" in text:
        signal("Frontend-heavy signal.", -10)
    if is_mobile_engineering_role(text):
        signal("Mobile-heavy signal.", -10)
    if "data engineer" in text and not has_backend_or_platform_signal(text.replace("data engineer", "")):
        signal("Data-engineering-only signal.", -10)
    if has_any(text, ("consulting", "client services", "agency")):
        signal("Consulting/client-services signal.", -10)
    if "rust" in text and "rust" not in skills_text:
        signal("Rust-only risk signal.", -10)
    if has_any(text, ("hardware", "firmware", "embedded")):
        signal("Hardware/firmware signal.", -15)
    if has_any(text, ("gpu", "cuda", "ptx", "kernel optimization")):
        signal("Narrow GPU/CUDA/kernel signal.", -15)

    return {
        "decision": decision,
        "reasons": reasons,
        "signals": signals,
        "heuristic_score": int(score),
    }


def prefilter_comments(comments: list[JsonDict], profile: JsonDict) -> list[JsonDict]:
    results: list[JsonDict] = []
    progress = tqdm(comments, desc="Prefiltering", unit="comment")  # type: ignore[misc]
    for comment in progress:
        result = prefilter_comment(comment, profile)
        results.append(
            {
                "comment_id": comment.get("id"),
                "text_hash": comment_text_hash(comment),
                "hn_url": comment.get("hn_url"),
                "position_index": comment.get("position_index"),
                "prefilter": result,
            }
        )
    return results


def write_prefilter_results(path: Path, *, month: str, results: list[JsonDict]) -> None:
    counts = comment_prefilter_counts(results)
    write_json_file(
        path,
        {
            "cache_version": 1,
            "month": month,
            "updated_at": utc_now_iso(),
            "counts": counts,
            "items": {as_string(item.get("comment_id")): item for item in results},
        },
    )


def extraction_comments_from_prefilter(
    comments: list[JsonDict],
    prefilter_results: list[JsonDict],
    *,
    include_deprioritized: bool,
    max_extractions: int | None = None,
) -> list[JsonDict]:
    prefilter_by_id = {
        item.get("comment_id"): item.get("prefilter") if isinstance(item.get("prefilter"), dict) else {}
        for item in prefilter_results
    }
    decisions = {
        comment_id: as_string(prefilter.get("decision"), "keep")
        for comment_id, prefilter in prefilter_by_id.items()
    }
    allowed = {"keep"}
    if include_deprioritized:
        allowed.add("deprioritize")
    candidates = [comment for comment in comments if decisions.get(comment.get("id"), "keep") in allowed]

    def sort_key(comment: JsonDict) -> tuple[int, int, int]:
        prefilter = prefilter_by_id.get(comment.get("id"), {})
        decision = as_string(prefilter.get("decision"), "keep")
        decision_rank = 0 if decision == "keep" else 1
        score = int(as_number_or_none(prefilter.get("heuristic_score")) or 0)
        position = int(as_number_or_none(comment.get("position_index")) or 1_000_000)
        return (decision_rank, -score, position)

    candidates.sort(key=sort_key)
    if max_extractions is not None:
        return candidates[:max_extractions]
    return candidates


def count_reusable_extractions(
    comments: list[JsonDict],
    cache: JsonDict,
    *,
    dry_run: bool,
    force: bool,
) -> tuple[int, int]:
    if force:
        return 0, len(comments)
    items = cache.get("items") if isinstance(cache.get("items"), dict) else {}
    reusable = 0
    for comment in comments:
        cached = items.get(str(comment.get("id")))
        if cache_entry_reusable(
            cached,
            expected_hash=comment_text_hash(comment),
            allow_dry_run=dry_run,
        ):
            reusable += 1
    return reusable, len(comments) - reusable


def profile_primary_skill_terms(profile: JsonDict) -> set[str]:
    skills = profile.get("skills")
    if not isinstance(skills, dict):
        return set()
    terms: set[str] = set()
    for section in ("strongest", "comfortable"):
        for item in as_string_list(skills.get(section)):
            lowered = item.lower()
            terms.add(lowered)
            for token in re.findall(r"[a-z0-9+#.]+", lowered):
                if len(token) >= 2:
                    terms.add(token)
    return terms


def has_stack_overlap(job: JsonDict, profile: JsonDict) -> bool:
    role = job.get("role") if isinstance(job.get("role"), dict) else {}
    stack = " ".join(as_string_list(role.get("tech_stack"))).lower()
    if not stack:
        return False
    skill_terms = profile_primary_skill_terms(profile)
    return any(term and term in stack for term in skill_terms)


def is_remote_region_eligible(text: str, profile: JsonDict) -> bool:
    restrictions = detect_geo_restrictions(text)
    compatible, _ = is_geo_compatible(restrictions, profile)
    return "remote" in text and compatible


def salary_text_for_job(job: JsonDict) -> str:
    role = job.get("role") if isinstance(job.get("role"), dict) else {}
    parts = [
        role.get("salary_raw"),
        role.get("currency"),
        role.get("equity"),
        role.get("title"),
        job.get("company_name"),
        " ".join(as_string_list(role.get("requirements"))),
        " ".join(as_string_list(role.get("nice_to_haves"))),
        " ".join(as_string_list(role.get("evidence_quotes"))),
    ]
    return " ".join(as_string(part) for part in parts if part is not None)


def salary_amount_to_number(value: str, suffix: str | None = None) -> float | None:
    cleaned = value.replace(",", "").strip()
    if not cleaned:
        return None
    try:
        number = float(cleaned)
    except ValueError:
        return None
    if suffix and suffix.lower() == "k":
        return number * 1000
    return number


def parse_salary_numbers(text: str) -> JsonDict:
    normalized = text.replace("—", "-").replace("–", "-")
    lowered = normalized.lower()
    is_hourly = has_regex(lowered, r"(?:/|\bper\s+)(?:hr|hour)\b|\bhourly\b")
    parsed: JsonDict = {
        "salary_min": None,
        "salary_max": None,
        "currency": None,
        "is_hourly": is_hourly,
        "is_annual": not is_hourly,
    }

    money = r"(?P<currency>[$€£])\s*(?P<first>\d[\d,]*(?:\.\d+)?)\s*(?P<first_suffix>k)?"
    range_pattern = (
        money
        + r"\s*(?:-|to)\s*(?P<second_currency>[$€£])?\s*"
        + r"(?P<second>\d[\d,]*(?:\.\d+)?)\s*(?P<second_suffix>k)?"
    )
    range_match = re.search(range_pattern, normalized, flags=re.IGNORECASE)
    if range_match:
        first_suffix = range_match.group("first_suffix")
        second_suffix = range_match.group("second_suffix")
        if not first_suffix and second_suffix:
            first_suffix = second_suffix
        salary_min = salary_amount_to_number(range_match.group("first"), first_suffix)
        salary_max = salary_amount_to_number(range_match.group("second"), second_suffix)
        parsed.update(
            {
                "salary_min": salary_min,
                "salary_max": salary_max,
                "currency": range_match.group("currency") or range_match.group("second_currency"),
            }
        )
        return parsed

    single_match = re.search(money, normalized, flags=re.IGNORECASE)
    if single_match:
        value = salary_amount_to_number(single_match.group("first"), single_match.group("first_suffix"))
        parsed.update(
            {
                "salary_min": value,
                "salary_max": value,
                "currency": single_match.group("currency"),
            }
        )
    return parsed


def has_sales_role_signal(job: JsonDict) -> bool:
    text = role_text(job)
    return has_any(text, ("sales", "account executive", "account manager", "business development"))


def has_salary_disclosed(job: JsonDict) -> bool:
    role = job.get("role") if isinstance(job.get("role"), dict) else {}
    salary_raw = as_string(role.get("salary_raw")).strip()
    salary_raw_lower = salary_raw.lower()
    if salary_raw and salary_raw_lower not in {"not disclosed", "n/a", "none", "unknown"}:
        if not has_any(salary_raw_lower, ("no salary", "no cash", "equity only", "equity-only")):
            return True
    if as_number_or_none(role.get("salary_min")) is not None or as_number_or_none(role.get("salary_max")) is not None:
        return True

    text = salary_text_for_job(job)
    lowered = text.lower()
    if has_regex(text, r"[$€£]\s*\d[\d,]*(?:\.\d+)?\s*k?(?:\s*(?:-|–|—|to)\s*[$€£]?\s*\d[\d,]*(?:\.\d+)?\s*k?)?"):
        return True
    if has_any(lowered, ("base salary", "salary:", "compensation:")):
        return True
    return "ote" in lowered and has_sales_role_signal(job)


def parsed_salary_for_job(job: JsonDict) -> JsonDict:
    role = job.get("role") if isinstance(job.get("role"), dict) else {}
    parsed = parse_salary_numbers(salary_text_for_job(job))
    salary_min = as_number_or_none(role.get("salary_min"))
    salary_max = as_number_or_none(role.get("salary_max"))
    if salary_min is not None:
        parsed["salary_min"] = salary_min
    if salary_max is not None:
        parsed["salary_max"] = salary_max
    currency = as_string_or_none(role.get("currency"))
    if currency:
        parsed["currency"] = currency
    return parsed


def profile_compensation(profile: JsonDict) -> JsonDict:
    compensation = profile.get("compensation")
    return compensation if isinstance(compensation, dict) else {}


def salary_meets_profile_minimum(job: JsonDict, profile: JsonDict) -> bool:
    threshold = as_number_or_none(profile_hard_filters(profile).get("minimum_salary_if_disclosed"))
    if threshold is None:
        return False
    parsed = parsed_salary_for_job(job)
    if parsed.get("is_hourly") and not bool(profile_compensation(profile).get("annualize_hourly_rate")):
        return False
    salary_max = as_number_or_none(parsed.get("salary_max"))
    return salary_max is not None and salary_max >= threshold


def salary_below_profile_minimum(job: JsonDict, profile: JsonDict) -> bool:
    threshold = as_number_or_none(profile_hard_filters(profile).get("minimum_salary_if_disclosed"))
    if threshold is None or not has_salary_disclosed(job):
        return False
    parsed = parsed_salary_for_job(job)
    if parsed.get("is_hourly") and not bool(profile_compensation(profile).get("annualize_hourly_rate")):
        return False
    salary_max = as_number_or_none(parsed.get("salary_max"))
    return salary_max is not None and salary_max < threshold


def hourly_salary_below_profile_minimum(job: JsonDict, profile: JsonDict) -> bool:
    compensation = profile_compensation(profile)
    minimum_hourly = as_number_or_none(compensation.get("minimum_hourly_usd"))
    if minimum_hourly is None:
        return False
    parsed = parsed_salary_for_job(job)
    if not parsed.get("is_hourly"):
        return False
    hourly_max = as_number_or_none(parsed.get("salary_max"))
    return hourly_max is not None and hourly_max < minimum_hourly


def has_high_quality_post_signal(job: JsonDict) -> bool:
    role = job.get("role") if isinstance(job.get("role"), dict) else {}
    title = as_string(role.get("title"))
    has_apply = bool(as_string_or_none(role.get("apply_url")) or as_string_or_none(role.get("apply_email")))
    has_stack = bool(as_string_list(role.get("tech_stack")))
    has_requirements = bool(as_string_list(role.get("requirements")))
    clear_title = bool(title and title != "Unknown role")
    return has_apply and clear_title and (has_stack or has_requirements)


def role_is_mid_only(title: str, text: str) -> bool:
    if has_regex(text, r"\b(?:senior|staff|principal|founding|lead)\b"):
        return False
    return has_regex(title, r"\b(?:mid|mid-level|mid level)\b")


def title_is_data_engineer_only(title: str, text: str) -> bool:
    if "data engineer" not in title:
        return False
    return not has_regex(text, r"\b(?:backend|back-end|platform|infrastructure|api|sre|full stack|full-stack|fullstack)\b")


def profile_preference_text(value: Any) -> str:
    if isinstance(value, list):
        return " ".join(as_string(item) for item in value)
    if isinstance(value, dict):
        return canonical_json(value)
    if isinstance(value, str):
        return value
    return ""


def candidate_targets_backend_platform(profile: JsonDict) -> bool:
    role_preferences = profile.get("role_preferences")
    prefer = role_preferences.get("prefer") if isinstance(role_preferences, dict) else None
    text = " ".join(
        (
            profile_preference_text(profile.get("target_roles")),
            profile_preference_text(prefer),
        )
    ).lower()
    return has_any(
        text,
        (
            "backend",
            "platform",
            "infrastructure",
            "principal",
            "staff",
            "distributed",
            "api",
            "cloud",
            "product engineering",
        ),
    )



def is_language_only_mismatch(text: str, profile: JsonDict, language: str) -> bool:
    skills_text = profile_skills_text(profile)
    if language in skills_text:
        return False
    return language in text and not has_regex(
        text,
        r"\b(?:python|java|javascript|typescript|node|go|ruby|backend|platform|full stack|full-stack|fullstack)\b",
    )


def required_stack_mismatch(text: str, profile: JsonDict) -> str | None:
    skills_text = profile_skills_text(profile)
    checks = (
        ("Laravel", r"\blaravel\b", ("laravel", "php")),
        ("Rust", r"\brust\b", ("rust",)),
        ("Elixir", r"\belixir\b", ("elixir",)),
    )
    for label, pattern, acceptable in checks:
        if not has_regex(text, pattern):
            continue
        if any(skill in skills_text for skill in acceptable):
            continue
        required_context = has_regex(
            text,
            rf"\b(?:required|must have|expert|strong need|required stack|only)\b.{{0,60}}\b{label.lower()}\b"
            rf"|\b{label.lower()}\b.{{0,60}}\b(?:required|must have|expert|strong need|only)\b",
        )
        if required_context or is_language_only_mismatch(text, profile, label.lower()):
            return label
    return None


def is_frontend_heavy(job: JsonDict) -> bool:
    text = combined_hard_filter_text(job)
    return has_regex(text, r"\b(?:frontend|front-end|react|vue|angular|ui engineer)\b")


def is_mobile_heavy(job: JsonDict) -> bool:
    return is_mobile_engineering_role(combined_hard_filter_text(job))


def signal_has_seniority(title: str) -> bool:
    return has_regex(title, r"\b(?:staff|principal|founding|senior|sr\.?|lead)\b")


def heuristic_score_job(job: JsonDict, profile: JsonDict) -> JsonDict:
    role = job.get("role") if isinstance(job.get("role"), dict) else {}
    text = heuristic_job_text(job)
    title = as_string(role.get("title")).lower()
    remote_status = normalize_remote_status(role.get("remote_status"))
    filters = profile_hard_filters(profile)
    avoid_text = profile_avoid_text(profile)
    skills_text = profile_skills_text(profile)
    geo_restrictions = detect_geo_restrictions(text)
    geo_compatible, geo_reason = is_geo_compatible(geo_restrictions, profile)
    timezone_ok, timezone_reason = timezone_compatible(text, profile)

    score = 40
    score_cap = 100
    positive: list[str] = []
    negative: list[str] = []

    def add_positive(points: int, signal: str) -> None:
        nonlocal score
        score += points
        positive.append(f"+{points} {signal}")

    def add_negative(points: int, signal: str) -> None:
        nonlocal score
        score -= points
        negative.append(f"-{points} {signal}")

    def cap(max_score: int, reason: str) -> None:
        nonlocal score_cap
        if max_score < score_cap:
            score_cap = max_score
            negative.append(f"cap {max_score} {reason}")

    incompatible_geo = not geo_compatible
    onsite_or_hybrid_blocked = False
    salary_missing = not has_salary_disclosed(job)
    annual_salary_below_minimum = salary_below_profile_minimum(job, profile)
    hourly_below_minimum = hourly_salary_below_profile_minimum(job, profile)
    stack_mismatch = required_stack_mismatch(text, profile)
    data_engineering_only = title_is_data_engineer_only(title, text) and candidate_targets_backend_platform(profile)
    consulting_services = has_any(text, ("consulting", "client services", "agency")) and has_any(
        avoid_text,
        ("consulting", "agency", "services"),
    )
    mobile_heavy = is_mobile_heavy(job)
    frontend_heavy = is_frontend_heavy(job)

    if "remote" in text:
        if incompatible_geo:
            add_negative(100, f"incompatible geo restriction: {geo_reason}")
            cap(0, "incompatible geo restriction")
        elif is_worldwide_remote_text(text):
            add_positive(20, "remote worldwide/global/anywhere/any timezone")
        elif geo_restrictions.get("is_region_locked"):
            add_positive(15, "region-compatible remote")
        else:
            add_positive(15, "generic remote")

    if has_regex(title, r"\b(?:staff|principal|founding)\b"):
        add_positive(12, "staff/principal/founding title")
    if has_regex(title, r"\b(?:senior|sr\.?|lead)\b"):
        add_positive(10, "senior/lead title")

    if has_regex(
        f"{title} {text}",
        r"\b(?:backend|back-end|platform|infrastructure|distributed systems?|api|cloud|sre|retrieval)\b",
    ):
        add_positive(15, "backend/platform/infrastructure/distributed/API/cloud/SRE/retrieval")
    if has_regex(text, r"\b(?:ai|llm|rag|agents?|devtools?|developer tools|automation|b2b saas|productivity)\b"):
        add_positive(10, "AI/LLM/RAG/agents/devtools/automation/B2B SaaS/productivity")
    if has_stack_overlap(job, profile):
        add_positive(10, "strong stack overlap with profile skills")
    if salary_meets_profile_minimum(job, profile):
        add_positive(8, "salary disclosed and annual max meets minimum")
    if has_high_quality_post_signal(job):
        add_positive(5, "high-quality post with clear title, apply info, and stack/responsibilities")

    if filters.get("reject_wrong_geo") and incompatible_geo and "remote" not in text:
        add_negative(100, f"incompatible geo restriction: {geo_reason}")
        cap(0, "incompatible geo restriction")
    if not timezone_ok:
        add_negative(15, f"timezone window incompatible: {timezone_reason}")
        cap(75, "timezone window incompatible")
    allowed_hybrid_days = as_number_or_none(filters.get("reject_hybrid_more_than_days_per_week"))
    detected_days = detect_hybrid_days(text)
    if is_clearly_onsite_only(text) or (remote_status == "onsite" and not offers_remote_option(text)):
        onsite_or_hybrid_blocked = True
        add_negative(100, "onsite-only or required onsite role")
    elif remote_status == "hybrid" and allowed_hybrid_days is not None:
        if allowed_hybrid_days == 0 or (detected_days is not None and detected_days > allowed_hybrid_days):
            onsite_or_hybrid_blocked = True
            add_negative(100, "hybrid requirement exceeds allowed limit")
    if onsite_or_hybrid_blocked:
        cap(0, "onsite-only or hybrid above allowed limit")

    if is_mixed_seniority_role(job):
        add_negative(10, "mixed seniority posting; verify senior-level opening exists")
    elif has_regex(text, r"\b(?:intern|internship|junior|entry level|entry-level|new grad|graduate engineer)\b") or role_is_mid_only(title, text):
        add_negative(30, "junior/intern signal")
    if has_any(text, ("equity only", "equity-only", "unpaid", "no salary", "no cash compensation")):
        add_negative(50, "unpaid/equity-only")
    if is_mobile_only(job):
        add_negative(25, "mobile-only")
    if is_frontend_only(job):
        add_negative(25, "frontend-only")
    if is_non_engineering(job, profile):
        add_negative(25, "pure non-engineering role signal")
    if has_regex(text, r"\b(?:hardware|firmware|mechanical)\b") and not has_backend_or_platform_signal(text):
        add_negative(25, "hardware/firmware/mechanical-heavy")
    if (
        has_regex(text, r"\b(?:gpu|cuda|ptx|kernel)\b")
        and not has_any(skills_text, ("gpu", "cuda", "kernel"))
        and not has_backend_or_platform_signal(text)
    ):
        add_negative(25, "GPU/CUDA/kernel-only unless profile opts in")
    if consulting_services:
        add_negative(20, "pure consulting/client-services")
    if data_engineering_only:
        add_negative(20, "data-engineering-only while profile prefers backend/platform")
    if stack_mismatch:
        add_negative(15, f"required stack mismatch: {stack_mismatch}")
    if annual_salary_below_minimum:
        add_negative(40, "salary max below annual minimum")
    if hourly_below_minimum:
        parsed = parsed_salary_for_job(job)
        currency = as_string(parsed.get("currency"))
        low = as_number_or_none(parsed.get("salary_min"))
        high = as_number_or_none(parsed.get("salary_max"))
        if low is not None and high is not None and low != high:
            hourly = f"{currency}{low:g}-{high:g}/hr"
        else:
            value = high if high is not None else low
            hourly = f"{currency}{value:g}/hr" if value is not None else "hourly rate"
        add_negative(35, f"hourly rate below minimum: {hourly}")
    if salary_missing:
        add_negative(10, "no salary disclosed")
    if not as_string_list(role.get("tech_stack")) and not as_string_list(role.get("requirements")):
        add_negative(10, "vague post, no tech stack, or no requirements")

    if annual_salary_below_minimum:
        cap(45, "salary max below annual minimum")
    if salary_missing:
        cap(85, "salary missing")
    if not signal_has_seniority(title):
        cap(75, "no seniority signal")
    if not as_string_list(role.get("tech_stack")):
        cap(85, "tech stack missing")
    if stack_mismatch:
        cap(70, f"required stack mismatch: {stack_mismatch}")
    if data_engineering_only:
        cap(65, "data-engineering-only")
    if consulting_services:
        cap(75, "consulting/client-services")
    if (mobile_heavy or frontend_heavy) and not (is_mobile_only(job) or is_frontend_only(job)):
        cap(70, "mobile/frontend-heavy but not only")

    score = min(score, score_cap)
    return {
        "score": int(max(0, min(100, score))),
        "positive_signals": positive,
        "negative_signals": negative,
    }


def job_position_index(job: JsonDict) -> int:
    source = job.get("source_comment") if isinstance(job.get("source_comment"), dict) else {}
    return int(as_number_or_none(source.get("position_index")) or 1_000_000)


def job_saved_heuristic_score(job: JsonDict, profile: JsonDict) -> float:
    heuristic = job.get("heuristic") if isinstance(job.get("heuristic"), dict) else None
    if heuristic is None:
        heuristic = heuristic_score_job(job, profile)
        job["heuristic"] = heuristic
    return as_number_or_none(heuristic.get("score")) or 0.0


def job_company_title_key(job: JsonDict) -> str:
    role = job.get("role") if isinstance(job.get("role"), dict) else {}
    return f"{as_string(job.get('company_name')).lower()}|{as_string(role.get('title')).lower()}"


def sorted_jobs_for_matching(jobs: list[JsonDict], profile: JsonDict) -> list[JsonDict]:
    return sorted(
        jobs,
        key=lambda job: (
            -job_saved_heuristic_score(job, profile),
            job_position_index(job),
            job_company_title_key(job),
            as_string(job.get("job_id")),
        ),
    )


def plan_matching_jobs(
    *,
    jobs: list[JsonDict],
    profile: JsonDict,
    profile_hash: str,
    cache: JsonDict,
    dry_run: bool,
    force: bool,
    top_n_for_matching: int | None,
) -> JsonDict:
    items = cache.get("items") if isinstance(cache.get("items"), dict) else {}
    hard_filtered_jobs: list[JsonDict] = []
    eligible_jobs: list[JsonDict] = []

    for job in jobs:
        job["heuristic"] = heuristic_score_job(job, profile)
        hard_filter = apply_hard_filters(job, profile)
        if hard_filter.get("rejected"):
            hard_filtered_jobs.append(job)
            continue

        eligible_jobs.append(job)

    sorted_eligible_jobs = sorted_jobs_for_matching(eligible_jobs, profile)
    if top_n_for_matching is None:
        selected_eligible_jobs = sorted_eligible_jobs
        skipped_eligible_jobs: list[JsonDict] = []
    else:
        selected_eligible_jobs = sorted_eligible_jobs[:top_n_for_matching]
        skipped_eligible_jobs = sorted_eligible_jobs[top_n_for_matching:]

    cached_match_jobs: list[JsonDict] = []
    new_match_jobs: list[JsonDict] = []
    for job in selected_eligible_jobs:
        job_id = as_string(job.get("job_id"))
        cached = items.get(job_id)
        if not force and match_cache_entry_reusable(
            cached,
            job_hash=as_string(job.get("job_hash")),
            profile_hash=profile_hash,
            allow_dry_run=dry_run,
        ):
            cached_match_jobs.append(job)
        else:
            new_match_jobs.append(job)

    for job in selected_eligible_jobs:
        job["match_status"] = "llm_ranked"
    for job in skipped_eligible_jobs:
        job["match_status"] = "not_llm_ranked"
        job["not_ranked_reason"] = "Outside top N after heuristic sorting"

    selected_jobs = hard_filtered_jobs + selected_eligible_jobs
    return {
        "selected_jobs": selected_jobs,
        "hard_filtered_jobs": hard_filtered_jobs,
        "eligible_jobs": sorted_eligible_jobs,
        "selected_eligible_jobs": selected_eligible_jobs,
        "cached_match_jobs": cached_match_jobs,
        "new_match_jobs": new_match_jobs,
        "skipped_new_match_jobs": skipped_eligible_jobs,
    }


def budget_skipped_match_entries(
    jobs: list[JsonDict],
    *,
    profile_hash: str,
    profile: JsonDict,
) -> list[JsonDict]:
    entries: list[JsonDict] = []
    for job in jobs:
        heuristic = job.get("heuristic") if isinstance(job.get("heuristic"), dict) else heuristic_score_job(job, profile)
        entries.append(
            {
                "job_id": as_string(job.get("job_id")),
                "job_hash": as_string(job.get("job_hash")),
                "profile_hash": profile_hash,
                "source": "matching_budget_skipped",
                "match_status": "not_llm_ranked",
                "not_ranked_reason": "Outside top N after heuristic sorting",
                "model": None,
                "created_at": utc_now_iso(),
                "hard_filter": {"rejected": False, "reasons": []},
                "heuristic": heuristic,
                "raw_content": None,
                "parse_error": None,
                "match": normalize_match(
                    {
                        "score": 0,
                        "confidence": "low",
                        "bucket": "skip",
                        "summary": "Skipped by --top-n-for-matching budget before LLM matching.",
                        "positive_reasons": [],
                        "concerns": ["Not sent for LLM matching in this run."],
                        "missing_info": ["Run again with a higher --top-n-for-matching value to score this role."],
                        "evidence_quotes": [],
                        "suggested_action": "Skipped due to matching call budget.",
                    }
                ),
            }
        )
    return entries


def format_debug_job(job: JsonDict, rank: int) -> str:
    role = job.get("role") if isinstance(job.get("role"), dict) else {}
    heuristic = job.get("heuristic") if isinstance(job.get("heuristic"), dict) else {}
    return (
        f"{rank}. score={heuristic.get('score', 'N/A')} | "
        f"{value_or_unknown(job.get('company_name'))} | "
        f"{value_or_unknown(role.get('title'))} | "
        f"{format_location(role)} | {format_job_salary(job)}"
    )


def print_matching_selection_debug(
    *,
    selected_jobs: list[JsonDict],
    skipped_jobs: list[JsonDict],
    max_rows: int = 20,
) -> None:
    print("")
    print("Top heuristic jobs selected for LLM matching:")
    if not selected_jobs:
        print("- none")
    else:
        for rank, job in enumerate(selected_jobs[:max_rows], start=1):
            print(f"- {format_debug_job(job, rank)}")
        if len(selected_jobs) > max_rows:
            print(f"- ... showing first {max_rows} of {len(selected_jobs)} selected jobs")

    print("")
    print("Highest heuristic jobs not selected:")
    if not skipped_jobs:
        print("- none")
    else:
        for rank, job in enumerate(skipped_jobs[:max_rows], start=1):
            role = job.get("role") if isinstance(job.get("role"), dict) else {}
            heuristic = job.get("heuristic") if isinstance(job.get("heuristic"), dict) else {}
            print(
                f"- {rank}. score={heuristic.get('score', 'N/A')} | "
                f"{value_or_unknown(job.get('company_name'))} | "
                f"{value_or_unknown(role.get('title'))}"
            )
        if len(skipped_jobs) > max_rows:
            print(f"- ... showing first {max_rows} of {len(skipped_jobs)} not selected jobs")


def print_heuristic_details(jobs: list[JsonDict], *, max_rows: int = 50) -> None:
    print("")
    print(f"Top {min(max_rows, len(jobs))} heuristic jobs with signals:")
    if not jobs:
        print("- none")
        return
    for rank, job in enumerate(jobs[:max_rows], start=1):
        role = job.get("role") if isinstance(job.get("role"), dict) else {}
        heuristic = job.get("heuristic") if isinstance(job.get("heuristic"), dict) else {}
        positive = as_string_list(heuristic.get("positive_signals"))
        negative = as_string_list(heuristic.get("negative_signals"))
        print(
            f"{rank}. score={heuristic.get('score', 'N/A')} | "
            f"{value_or_unknown(job.get('company_name'))} | "
            f"{value_or_unknown(role.get('title'))} | "
            f"{format_location(role)} | {format_job_salary(job)}"
        )
        print(f"   + {'; '.join(positive) if positive else 'none'}")
        print(f"   - {'; '.join(negative) if negative else 'none'}")


def heuristic_score_from_job(job: JsonDict) -> float | None:
    heuristic = job.get("heuristic") if isinstance(job.get("heuristic"), dict) else {}
    return as_number_or_none(heuristic.get("score"))


def selection_inconsistency_warning(
    selected_jobs: list[JsonDict],
    skipped_jobs: list[JsonDict],
) -> str | None:
    ranked_scores = [score for job in selected_jobs if (score := heuristic_score_from_job(job)) is not None]
    skipped_scores = [score for job in skipped_jobs if (score := heuristic_score_from_job(job)) is not None]
    if not ranked_scores or not skipped_scores:
        return None
    min_ranked = min(ranked_scores)
    max_skipped = max(skipped_scores)
    if max_skipped > min_ranked:
        return (
            "WARNING: Selection inconsistency detected: "
            f"max not-selected heuristic score {max_skipped:g} exceeds "
            f"min selected heuristic score {min_ranked:g}."
        )
    return None


def print_budget_summary(title: str, rows: list[tuple[str, Any]]) -> None:
    print("")
    print(title)
    for label, value in rows:
        print(f"- {label}: {value}")


def confirm_llm_calls(total_new_calls: int, *, yes: bool) -> bool:
    if total_new_calls <= 50 or yes:
        return True
    answer = input(f"Proceed with approximately {total_new_calls} new LLM calls? [y/N] ")
    return answer.strip().lower() in {"y", "yes"}


def is_mixed_seniority_role(job: JsonDict) -> bool:
    role = job.get("role") if isinstance(job.get("role"), dict) else {}
    title = as_string(role.get("title")).lower()
    seniority = [item.lower() for item in as_string_list(role.get("seniority"))]
    return (
        "mixed" in seniority
        or is_mixed_seniority_text(title)
        or is_mixed_seniority_text(combined_hard_filter_text(job))
    )


def is_junior_or_intern(job: JsonDict) -> tuple[bool, str | None]:
    junior_or_intern, reason = is_junior_or_intern_post(combined_hard_filter_text(job))
    if reason == "Internship role.":
        return True, "internship role"
    if reason == "Mixed seniority posting; verify senior-level opening exists.":
        return False, None
    if junior_or_intern:
        return True, "junior or entry-level role"
    return False, None


def is_frontend_only(job: JsonDict) -> bool:
    title = title_text(job)
    text = combined_hard_filter_text(job)
    combined = f"{title} {text}"
    frontend_engineer = re.search(r"\b(?:front[\s-]?end|frontend)\s+engineer\b", combined)
    broader = (
        "full stack",
        "full-stack",
        "fullstack",
        "backend",
        "back-end",
        "platform",
        "infrastructure",
        "api",
        "server-side",
    )
    return frontend_engineer is not None and not has_any(combined, broader)


def is_mobile_only(job: JsonDict) -> bool:
    title = title_text(job)
    text = combined_hard_filter_text(job)
    combined = f"{title} {text}"
    broader = (
        "full stack",
        "full-stack",
        "fullstack",
        "backend",
        "back-end",
        "platform",
        "infrastructure",
        "api",
        "server-side",
    )
    return is_mobile_engineering_role(combined) and not has_any(combined, broader)


def avoids_pure_management(profile: JsonDict) -> bool:
    avoid_roles = profile.get("avoid_roles")
    if not isinstance(avoid_roles, list):
        return False
    avoid_text = " ".join(as_string(item).lower() for item in avoid_roles)
    return "pure engineering manager" in avoid_text or "pure people management" in avoid_text


def is_non_engineering(job: JsonDict, profile: JsonDict) -> bool:
    title = title_text(job)
    non_engineering_titles = (
        "technical program manager",
        "product manager",
        "program manager",
        "project manager",
        "designer",
        "sales",
        "account executive",
        "customer success",
        "recruiter",
        "marketing",
        "tpm",
    )
    if has_any(title, non_engineering_titles):
        return True
    if (
        "engineering manager" in title
        and avoids_pure_management(profile)
        and not has_any(title, ("hands-on", "hands on", "coding"))
    ):
        return True
    return False


def is_unpaid_or_equity_only(job: JsonDict) -> tuple[bool, bool]:
    text = combined_hard_filter_text(job)
    unpaid = re.search(r"\bunpaid\b", text) is not None
    equity_only = has_any(
        text,
        ("equity only", "equity-only", "no salary", "no cash", "no cash compensation"),
    )
    return unpaid, equity_only


def salary_below_minimum(job: JsonDict, minimum: Any) -> bool:
    threshold = as_number_or_none(minimum)
    if threshold is None:
        return False
    parsed = parsed_salary_for_job(job)
    currency = as_string(parsed.get("currency")).upper()
    salary_max = as_number_or_none(parsed.get("salary_max"))

    if currency and currency not in {"USD", "$"}:
        return False
    if parsed.get("is_hourly"):
        return False

    return salary_max is not None and salary_max < threshold


def has_geo_conflict(job: JsonDict, profile: JsonDict) -> bool:
    text = combined_hard_filter_text(job)
    restrictions = detect_geo_restrictions(text)
    compatible, _ = is_geo_compatible(restrictions, profile)
    return not compatible


def apply_hard_filters(job: JsonDict, profile: JsonDict) -> JsonDict:
    """Apply conservative deterministic filters.

    These checks reject only clear mismatches. Vague or ambiguous cases should
    continue to LLM ranking instead of being hard-rejected here.
    """
    filters = profile_hard_filters(profile)
    reasons: list[str] = []
    role = job.get("role") if isinstance(job.get("role"), dict) else {}
    remote_status = normalize_remote_status(role.get("remote_status"))
    text = combined_hard_filter_text(job)

    junior_or_intern, junior_reason = is_junior_or_intern(job)
    if filters.get("reject_internships") and junior_reason == "internship role":
        reasons.append("Internship role.")
    if filters.get("reject_junior_roles") and junior_or_intern and junior_reason != "internship role":
        reasons.append("Junior or entry-level role.")

    if filters.get("reject_onsite_only") and (
        is_clearly_onsite_only(text) or (remote_status == "onsite" and not offers_remote_option(text))
    ):
        reasons.append("Onsite-only role.")

    allowed_hybrid_days = filters.get("reject_hybrid_more_than_days_per_week")
    has_hybrid_or_required_onsite = (
        remote_status == "hybrid"
        or "hybrid" in text
        or (
            not offers_remote_option(text)
            and has_regex(text, r"\b(?:onsite|on-site|in office|in-office)\b")
        )
    )
    if allowed_hybrid_days is not None and has_hybrid_or_required_onsite:
        detected_days = detect_hybrid_days(text)
        allowed = as_number_or_none(allowed_hybrid_days)
        if allowed == 0:
            reasons.append("Hybrid role is not allowed.")
        elif detected_days is not None and allowed is not None and detected_days > allowed:
            reasons.append(f"Hybrid requirement is {detected_days} days/week, above allowed limit.")

    unpaid, equity_only = is_unpaid_or_equity_only(job)
    if filters.get("reject_unpaid") and unpaid:
        reasons.append("Unpaid role.")
    if filters.get("reject_equity_only") and equity_only:
        reasons.append("Equity-only role.")

    if filters.get("reject_frontend_only") and is_frontend_only(job):
        reasons.append("Frontend-only role.")
    if filters.get("reject_mobile_only") and is_mobile_only(job):
        reasons.append("Mobile-only role.")
    if filters.get("reject_non_engineering_roles") and is_non_engineering(job, profile):
        reasons.append("Non-engineering role.")
    if filters.get("reject_wrong_geo"):
        geo_restrictions = detect_geo_restrictions(text)
        geo_compatible, geo_reason = is_geo_compatible(geo_restrictions, profile)
        if not geo_compatible:
            reasons.append(
                "Region-locked remote role incompatible with candidate profile: "
                + geo_reason
            )
    if salary_below_minimum(job, filters.get("minimum_salary_if_disclosed")):
        reasons.append("Disclosed salary is below configured minimum.")

    return {"rejected": bool(reasons), "reasons": reasons}


def bucket_from_score(score: float) -> str:
    if score >= 90:
        return "must_apply"
    if score >= 75:
        return "strong_maybe"
    if score >= 60:
        return "maybe"
    if score >= 40:
        return "weak_fit"
    return "skip"


def normalize_match_result(result: Any) -> JsonDict:
    data = result if isinstance(result, dict) else {}
    score = as_number_or_none(data.get("score"))
    if score is None:
        score = 0.0
    score = max(0.0, min(100.0, score))

    confidence = as_string(data.get("confidence"), "low").lower()
    if confidence not in VALID_CONFIDENCE:
        confidence = "low"

    bucket = as_string(data.get("bucket"), "").lower()
    if bucket not in VALID_BUCKETS:
        bucket = bucket_from_score(score)

    if "score" not in data and "bucket" not in data:
        bucket = "skip"

    return {
        "score": score,
        "confidence": confidence,
        "bucket": bucket,
        "summary": as_string(data.get("summary")),
        "positive_reasons": as_string_list(data.get("positive_reasons")),
        "concerns": as_string_list(data.get("concerns")),
        "missing_info": as_string_list(data.get("missing_info")),
        "evidence_quotes": as_string_list(data.get("evidence_quotes")),
        "suggested_action": as_string(data.get("suggested_action")),
    }


def normalize_match(data: Any) -> JsonDict:
    normalized = normalize_match_result(data)
    return normalized


def matching_system_prompt() -> str:
    return (
        "You rank Hacker News jobs against a candidate profile. Score from 0-100 "
        "using the profile scoring weights. Be ruthless: most jobs should not be "
        "must_apply. Do not assume unstated facts. Use exact evidence quotes from "
        "the job posting. Return valid JSON only."
    )


def matching_user_prompt(
    *,
    profile: JsonDict,
    job: JsonDict,
) -> str:
    return (
        "Score this extracted job against the candidate profile.\n\n"
        "Scoring guidance:\n"
        "- 90-100 must_apply\n"
        "- 75-89 strong_maybe\n"
        "- 60-74 maybe\n"
        "- 40-59 weak_fit\n"
        "- 0-39 skip\n"
        "- Be ruthless. Most jobs should not be must_apply.\n"
        "- Do not assume unstated facts.\n"
        "- Use exact evidence quotes from the job posting.\n\n"
        "Return this JSON object:\n"
        "{\n"
        '  "score": number,\n'
        '  "confidence": "low"|"medium"|"high",\n'
        '  "bucket": "must_apply"|"strong_maybe"|"maybe"|"weak_fit"|"skip",\n'
        '  "summary": string,\n'
        '  "positive_reasons": string[],\n'
        '  "concerns": string[],\n'
        '  "missing_info": string[],\n'
        '  "evidence_quotes": string[],\n'
        '  "suggested_action": string\n'
        "}\n"
        "\n"
        "Candidate profile JSON:\n"
        f"{json.dumps(profile, indent=2, ensure_ascii=False)}\n\n"
        "Extracted job JSON:\n"
        f"{json.dumps(job, indent=2, ensure_ascii=False)}"
    )


def dry_run_match(job: JsonDict) -> JsonDict:
    role = job.get("role") if isinstance(job.get("role"), dict) else {}
    evidence = as_string_list(role.get("evidence_quotes"))
    text = combined_hard_filter_text(job)
    score = 50
    positive_reasons: list[str] = []
    concerns: list[str] = []

    if "remote" in text:
        score += 20
        positive_reasons.append("Dry-run heuristic: remote appears in the post.")
    if re.search(r"\b(?:senior|staff|principal|founding)\b", text):
        score += 20
        positive_reasons.append("Dry-run heuristic: senior/staff/principal/founding appears.")
    if re.search(r"\b(?:backend|platform|ai|data)\b", text):
        score += 20
        positive_reasons.append("Dry-run heuristic: backend/platform/AI/data appears.")
    if is_clearly_onsite_only(text):
        score -= 30
        concerns.append("Dry-run heuristic: onsite-only appears.")
    if re.search(r"\b(?:junior|intern|internship)\b", text):
        score -= 30
        concerns.append("Dry-run heuristic: junior/intern appears.")

    score = max(0, min(100, score))
    data: JsonDict = {
        "score": score,
        "confidence": "low",
        "summary": "Dry-run heuristic score only. This is not real AI analysis.",
        "positive_reasons": positive_reasons
        or ["Dry-run heuristic found no strong positive signals."],
        "concerns": concerns
        or ["Dry-run mode uses simple keyword scoring and may miss important nuance."],
        "missing_info": ["LLM extraction and LLM matching were skipped."],
        "evidence_quotes": evidence[:3],
        "suggested_action": "Run again without --dry-run after configuring LLM_API_KEY and LLM_MODEL.",
    }
    return normalize_match(data)


def match_cache_entry_reusable(
    entry: Any,
    *,
    job_hash: str,
    profile_hash: str,
    allow_dry_run: bool,
) -> bool:
    if not isinstance(entry, dict):
        return False
    if entry.get("job_hash") != job_hash or entry.get("profile_hash") != profile_hash:
        return False
    if entry.get("source") == "matching_budget_skipped":
        return False
    if not isinstance(entry.get("match"), dict):
        return False
    return allow_dry_run or entry.get("source") == "llm"


def match_jobs(
    *,
    jobs: list[JsonDict],
    profile: JsonDict,
    profile_hash: str,
    cache: JsonDict,
    client: Any | None,
    model: str | None,
    dry_run: bool,
    force: bool,
) -> list[JsonDict]:
    items = cache.setdefault("items", {})
    if not isinstance(items, dict):
        items = {}
        cache["items"] = items

    results: list[JsonDict] = []
    progress = tqdm(jobs, desc="Matching", unit="job")  # type: ignore[misc]

    for job in progress:
        job_hash = as_string(job.get("job_hash"))
        job_id = as_string(job.get("job_id"))
        heuristic = job.get("heuristic") if isinstance(job.get("heuristic"), dict) else heuristic_score_job(job, profile)
        job["heuristic"] = heuristic
        hard_filter = apply_hard_filters(job, profile)
        cached = items.get(job_id)

        if hard_filter["rejected"]:
            entry = {
                "job_id": job_id,
                "job_hash": job_hash,
                "profile_hash": profile_hash,
                "source": "hard_filter",
                "match_status": "hard_filtered",
                "model": None,
                "created_at": utc_now_iso(),
                "hard_filter": hard_filter,
                "heuristic": heuristic,
                "raw_content": None,
                "parse_error": None,
                "match": None,
            }
            items[job_id] = entry
            results.append(entry)
            continue

        if not force and match_cache_entry_reusable(
            cached,
            job_hash=job_hash,
            profile_hash=profile_hash,
            allow_dry_run=dry_run,
        ):
            cached["hard_filter"] = hard_filter
            cached["heuristic"] = heuristic
            cached["match_status"] = "llm_ranked"
            cached.pop("not_ranked_reason", None)
            results.append(cached)
            continue

        created_at = utc_now_iso()
        raw_content: str | None = None
        parse_error: str | None = None

        if dry_run:
            match = dry_run_match(job)
            source = "dry_run_heuristic"
        else:
            try:
                parsed, raw_content = chat_json(
                    client=client,
                    model=str(model),
                    system_prompt=matching_system_prompt(),
                    user_prompt=matching_user_prompt(
                        profile=profile,
                        job=job,
                    ),
                )
                match = normalize_match(parsed)
                source = "llm"
            except LLMJsonParseError as exc:
                raise AnalyzeError(
                    f"LLM matching returned invalid JSON for job {job_id}. "
                    f"Model: {model}. {exc}"
                ) from exc
            except Exception as exc:
                raise AnalyzeError(
                    f"LLM matching request failed for job {job_id}. "
                    f"Model: {model}. Base URL: {configured_llm_base_url(client)}. "
                    f"{exception_detail(exc)}\n"
                    "Check that LLM_BASE_URL and LLM_MODEL are valid for this API key."
                ) from exc

        entry = {
            "job_id": job_id,
            "job_hash": job_hash,
            "profile_hash": profile_hash,
            "source": source,
            "match_status": "llm_ranked",
            "model": model,
            "created_at": created_at,
            "hard_filter": hard_filter,
            "heuristic": heuristic,
            "raw_content": raw_content,
            "parse_error": parse_error,
            "match": match,
        }
        items[job_id] = entry
        results.append(entry)

    cache["profile_hash"] = profile_hash
    cache["updated_at"] = utc_now_iso()
    return results


def outreach_word_limit(profile: JsonDict) -> int:
    outreach = profile.get("outreach")
    if isinstance(outreach, dict):
        limit = as_number_or_none(outreach.get("max_words"))
        if limit is not None and limit > 0:
            return int(limit)
    return 180


def normalize_outreach_result(result: Any) -> JsonDict:
    data = result if isinstance(result, dict) else {}
    return {
        "subject": as_string(data.get("subject")),
        "body": as_string(data.get("body")),
    }


def should_generate_outreach(entry: JsonDict) -> bool:
    hard_filter = entry.get("hard_filter") if isinstance(entry.get("hard_filter"), dict) else {}
    if hard_filter.get("rejected"):
        return False

    match = entry.get("match") if isinstance(entry.get("match"), dict) else {}
    score = as_number_or_none(match.get("score")) or 0.0
    bucket = as_string(match.get("bucket")).lower()
    return bucket == "must_apply" or score >= 85


def outreach_system_prompt() -> str:
    return (
        "You write concise, human-sounding application emails for senior software "
        "engineering roles. Be warm, specific, and confident. Do not overclaim. "
        "Do not sound like a generic cover letter. Return valid JSON only."
    )


def outreach_user_prompt(*, profile: JsonDict, job: JsonDict, match: JsonDict) -> str:
    max_words = outreach_word_limit(profile)
    return (
        "Generate a concise application email for this job.\n\n"
        "Requirements:\n"
        f"- Body must be under {max_words} words.\n"
        "- Warm, specific, confident.\n"
        "- Mention 1-2 specific details from the job post.\n"
        "- Connect those details to the candidate's background.\n"
        "- Do not sound like a generic cover letter.\n"
        "- Do not overclaim.\n"
        '- Do not say "I am the perfect fit."\n'
        "- End with a simple call to action.\n"
        "- Include a subject line.\n\n"
        "Return JSON exactly like:\n"
        "{\n"
        '  "subject": string,\n'
        '  "body": string\n'
        "}\n\n"
        "Candidate profile JSON:\n"
        f"{json.dumps(profile, indent=2, ensure_ascii=False)}\n\n"
        "Extracted job JSON:\n"
        f"{json.dumps(job, indent=2, ensure_ascii=False)}\n\n"
        "Match analysis JSON:\n"
        f"{json.dumps(match, indent=2, ensure_ascii=False)}"
    )


def generate_outreach_drafts(
    *,
    match_entries: list[JsonDict],
    jobs: list[JsonDict],
    profile: JsonDict,
    client: Any | None,
    model: str | None,
    dry_run: bool,
) -> None:
    if dry_run:
        return

    jobs_by_id = {as_string(job.get("job_id")): job for job in jobs}
    eligible_entries = [
        entry
        for entry in match_entries
        if should_generate_outreach(entry)
        and not isinstance(entry.get("outreach"), dict)
        and as_string(entry.get("job_id")) in jobs_by_id
    ]

    progress = tqdm(eligible_entries, desc="Outreach", unit="email")  # type: ignore[misc]
    for entry in progress:
        job = jobs_by_id[as_string(entry.get("job_id"))]
        match = entry.get("match") if isinstance(entry.get("match"), dict) else {}

        try:
            parsed, raw_content = chat_json(
                client=client,
                model=str(model),
                system_prompt=outreach_system_prompt(),
                user_prompt=outreach_user_prompt(
                    profile=profile,
                    job=job,
                    match=match,
                ),
            )
            outreach = normalize_outreach_result(parsed)
            if not outreach["subject"] or not outreach["body"]:
                raise AnalyzeError("Outreach response must include non-empty subject and body.")
            entry["outreach"] = outreach
            entry["outreach_model"] = model
            entry["outreach_created_at"] = utc_now_iso()
            entry["raw_outreach_content"] = raw_content
            entry["outreach_error"] = None
        except LLMJsonParseError as exc:
            entry["outreach"] = None
            entry["outreach_model"] = model
            entry["outreach_created_at"] = utc_now_iso()
            entry["raw_outreach_content"] = exc.raw_content
            entry["outreach_error"] = str(exc)
        except Exception as exc:
            entry["outreach"] = None
            entry["outreach_model"] = model
            entry["outreach_created_at"] = utc_now_iso()
            entry["raw_outreach_content"] = None
            entry["outreach_error"] = str(exc)


def value_or_unknown(value: Any) -> str:
    text = as_string_or_none(value)
    return text if text is not None else "Unknown"


def format_optional_number(value: float | int | None) -> str:
    if value is None:
        return "N/A"
    number = float(value)
    if number.is_integer():
        return str(int(number))
    return f"{number:.1f}"


def format_salary(role: JsonDict) -> str:
    salary_raw = as_string_or_none(role.get("salary_raw"))
    if salary_raw and salary_raw.lower() != "not disclosed":
        return salary_raw
    salary_min = as_number_or_none(role.get("salary_min"))
    salary_max = as_number_or_none(role.get("salary_max"))
    currency = as_string_or_none(role.get("currency")) or ""
    if salary_min is None and salary_max is None:
        return "Not disclosed"
    if salary_min is not None and salary_max is not None:
        return f"{currency} {salary_min:g}-{salary_max:g}".strip()
    if salary_min is not None:
        return f"{currency} {salary_min:g}+".strip()
    return f"{currency} up to {salary_max:g}".strip()


def format_job_salary(job: JsonDict) -> str:
    role = job.get("role") if isinstance(job.get("role"), dict) else {}
    formatted = format_salary(role)
    if formatted != "Not disclosed":
        return formatted

    parsed = parsed_salary_for_job(job)
    salary_min = as_number_or_none(parsed.get("salary_min"))
    salary_max = as_number_or_none(parsed.get("salary_max"))
    if salary_min is None and salary_max is None:
        return "Not disclosed"
    currency = as_string(parsed.get("currency"))
    suffix = "/hr" if parsed.get("is_hourly") else ""
    if salary_min is not None and salary_max is not None and salary_min != salary_max:
        return f"{currency}{salary_min:g}-{salary_max:g}{suffix}".strip()
    value = salary_max if salary_max is not None else salary_min
    return f"{currency}{value:g}{suffix}".strip()


def format_location(role: JsonDict) -> str:
    parts = [
        as_string_or_none(role.get("remote_status")),
        as_string_or_none(role.get("remote_region")),
        as_string_or_none(role.get("location_raw")),
    ]
    return " / ".join(part for part in parts if part) or "Unknown"


def markdown_list(items: list[str], *, empty: str = "None listed") -> str:
    if not items:
        return f"- {empty}"
    return "\n".join(f"- {item}" for item in items)


def markdown_quotes(items: list[str]) -> str:
    if not items:
        return "- None listed"
    return "\n".join(f"> {item}" for item in items)


def markdown_table_cell(value: Any) -> str:
    text = as_string(value, "Unknown")
    text = " ".join(text.split())
    return text.replace("|", "\\|")


def truncate_text(text: str, *, max_chars: int = 140) -> str:
    compact = " ".join(text.split())
    if len(compact) <= max_chars:
        return compact
    return compact[: max_chars - 3].rstrip() + "..."


def job_lookup(jobs: list[JsonDict]) -> dict[str, JsonDict]:
    return {as_string(job.get("job_id")): job for job in jobs}


def rendered_job_details(job: JsonDict, entry: JsonDict) -> str:
    role = job.get("role") if isinstance(job.get("role"), dict) else {}
    match = entry.get("match") if isinstance(entry.get("match"), dict) else None
    hard_filter = entry.get("hard_filter") if isinstance(entry.get("hard_filter"), dict) else {}
    source_comment = job.get("source_comment") if isinstance(job.get("source_comment"), dict) else {}

    company = value_or_unknown(job.get("company_name"))
    title = value_or_unknown(role.get("title"))
    hn_url = value_or_unknown(source_comment.get("hn_url"))
    apply_url = as_string_or_none(role.get("apply_url"))
    apply_email = as_string_or_none(role.get("apply_email"))
    apply = ", ".join(item for item in [apply_url, apply_email] if item) or "Not found"
    raw_hn_post = as_string(source_comment.get("plain_text"))
    tech_stack = as_string_list(role.get("tech_stack"))

    if match:
        score = match.get("score")
        bucket = as_string(match.get("bucket"))
        summary = as_string(match.get("summary"), "No summary.")
        positive_reasons = as_string_list(match.get("positive_reasons"))
        concerns = as_string_list(match.get("concerns"))
        missing_info = as_string_list(match.get("missing_info"))
        evidence_quotes = as_string_list(match.get("evidence_quotes")) or as_string_list(
            role.get("evidence_quotes")
        )
        suggested_action = as_string_or_none(match.get("suggested_action"))
    else:
        score = "N/A"
        bucket = "hard_filtered"
        summary = "Skipped by deterministic hard filters before LLM matching."
        positive_reasons = []
        concerns = []
        missing_info = []
        evidence_quotes = as_string_list(role.get("evidence_quotes"))
        suggested_action = None

    outreach = entry.get("outreach") if isinstance(entry.get("outreach"), dict) else None

    hard_reasons = as_string_list(hard_filter.get("reasons"))

    lines = [
        f"### {company} - {title}",
        "",
        f"- Score: {score}",
        f"- Bucket: {bucket}",
        f"- Location/remote: {format_location(role)}",
        f"- Salary: {format_job_salary(job)}",
        f"- Tech stack: {', '.join(tech_stack) if tech_stack else 'Not disclosed'}",
        f"- HN comment: {hn_url}",
        f"- Apply: {apply}",
        f"- Raw HN post: {json.dumps(raw_hn_post, ensure_ascii=False)}",
        "",
    ]

    if is_mixed_seniority_role(job):
        lines.extend(["Warning: Mixed seniority posting; verify senior-level opening exists.", ""])

    lines.extend(
        [
            f"Summary: {summary}",
            "",
            "Why it fits:",
            markdown_list(positive_reasons),
            "",
            "Concerns:",
            markdown_list(concerns),
            "",
            "Missing information:",
            markdown_list(missing_info),
            "",
            "Evidence quotes:",
            markdown_quotes(evidence_quotes),
        ]
    )

    if hard_reasons:
        lines.extend(["", "Hard filter reasons:", markdown_list(hard_reasons)])
    if suggested_action:
        lines.extend(["", f"Suggested action: {suggested_action}"])
    if outreach:
        lines.extend(
            [
                "",
                "### Draft Outreach",
                "",
                f"Subject: {as_string(outreach.get('subject'))}",
                "",
                "Body:",
                as_string(outreach.get("body")),
            ]
        )

    return "\n".join(lines)


def rendered_low_priority_job(job: JsonDict, entry: JsonDict) -> str:
    role = job.get("role") if isinstance(job.get("role"), dict) else {}
    heuristic = entry.get("heuristic") if isinstance(entry.get("heuristic"), dict) else {}
    company = value_or_unknown(job.get("company_name"))
    title = value_or_unknown(role.get("title"))
    score = heuristic.get("score", "N/A")
    positive = as_string_list(heuristic.get("positive_signals"))
    negative = as_string_list(heuristic.get("negative_signals"))
    lines = [
        f"### {company} - {title}",
        "",
        f"- Heuristic score: {score}",
        "",
    ]
    if is_mixed_seniority_role(job):
        lines.extend(["Warning: Mixed seniority posting; verify senior-level opening exists.", ""])
    lines.extend(
        [
            "Positive signals:",
            markdown_list(positive),
            "",
            "Negative signals:",
            markdown_list(negative),
        ]
    )
    return "\n".join(lines)


def compact_hard_filtered_table(pairs: list[tuple[JsonDict, JsonDict]]) -> str:
    if not pairs:
        return "No jobs in this section."
    lines = [
        "| Company | Title | Location | Salary | HN URL | Reasons |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for job, entry in pairs:
        role = job.get("role") if isinstance(job.get("role"), dict) else {}
        source = job.get("source_comment") if isinstance(job.get("source_comment"), dict) else {}
        hard_filter = entry.get("hard_filter") if isinstance(entry.get("hard_filter"), dict) else {}
        reasons = "; ".join(as_string_list(hard_filter.get("reasons")))
        if not reasons:
            match = entry.get("match") if isinstance(entry.get("match"), dict) else {}
            reasons = "; ".join(as_string_list(match.get("concerns"))) or as_string(match.get("summary"))
        lines.append(
            "| "
            + " | ".join(
                [
                    markdown_table_cell(job.get("company_name")),
                    markdown_table_cell(role.get("title")),
                    markdown_table_cell(format_location(role)),
                    markdown_table_cell(format_job_salary(job)),
                    markdown_table_cell(source.get("hn_url")),
                    markdown_table_cell(truncate_text(reasons or "Skipped.")),
                ]
            )
            + " |"
        )
    return "\n".join(lines)


def compact_weak_fit_table(pairs: list[tuple[JsonDict, JsonDict]]) -> str:
    if not pairs:
        return "No jobs in this section."
    lines = [
        "| Score | Company | Title | Location | Salary | HN URL | Main concern |",
        "| ---: | --- | --- | --- | --- | --- | --- |",
    ]
    for job, entry in pairs:
        role = job.get("role") if isinstance(job.get("role"), dict) else {}
        source = job.get("source_comment") if isinstance(job.get("source_comment"), dict) else {}
        match = entry.get("match") if isinstance(entry.get("match"), dict) else {}
        concerns = "; ".join(as_string_list(match.get("concerns")))
        lines.append(
            "| "
            + " | ".join(
                [
                    markdown_table_cell(match.get("score", "N/A")),
                    markdown_table_cell(job.get("company_name")),
                    markdown_table_cell(role.get("title")),
                    markdown_table_cell(format_location(role)),
                    markdown_table_cell(format_job_salary(job)),
                    markdown_table_cell(source.get("hn_url")),
                    markdown_table_cell(truncate_text(concerns or as_string(match.get("summary")))),
                ]
            )
            + " |"
        )
    return "\n".join(lines)


def compact_not_llm_ranked_table(pairs: list[tuple[JsonDict, JsonDict]]) -> str:
    if not pairs:
        return "No jobs in this section."
    lines = [
        "| Company | Title | Heuristic | Location | Salary | Positive signals | Negative signals |",
        "| --- | --- | ---: | --- | --- | --- | --- |",
    ]
    for job, entry in pairs:
        role = job.get("role") if isinstance(job.get("role"), dict) else {}
        heuristic = entry.get("heuristic") if isinstance(entry.get("heuristic"), dict) else {}
        positive = "; ".join(as_string_list(heuristic.get("positive_signals")))
        negative = "; ".join(as_string_list(heuristic.get("negative_signals")))
        lines.append(
            "| "
            + " | ".join(
                [
                    markdown_table_cell(job.get("company_name")),
                    markdown_table_cell(role.get("title")),
                    markdown_table_cell(heuristic.get("score", "N/A")),
                    markdown_table_cell(format_location(role)),
                    markdown_table_cell(format_job_salary(job)),
                    markdown_table_cell(truncate_text(positive or "None listed")),
                    markdown_table_cell(truncate_text(negative or "None listed")),
                ]
            )
            + " |"
        )
    return "\n".join(lines)


def generate_report(
    *,
    month: str,
    raw_data: JsonDict,
    comments_considered: int,
    prefilter_counts: JsonDict | None,
    extraction_llm_calls_made: int,
    extraction_cache_reused: int,
    extraction_entries: list[JsonDict],
    jobs: list[JsonDict],
    match_entries: list[JsonDict],
    output_path: Path,
    top_n_for_matching: int | None = None,
    report_mode: str = "shortlist",
    dry_run: bool = False,
) -> None:
    thread = raw_data.get("thread") if isinstance(raw_data.get("thread"), dict) else {}
    all_comments = raw_data.get("comments") if isinstance(raw_data.get("comments"), list) else []
    total_comments = thread.get("top_level_comment_count") or len(all_comments)
    extracted_job_posts = sum(
        1
        for entry in extraction_entries
        if isinstance(entry.get("extraction"), dict) and entry["extraction"].get("is_job_post")
    )
    hard_filtered_count = sum(
        1
        for entry in match_entries
        if isinstance(entry.get("hard_filter"), dict) and entry["hard_filter"].get("rejected")
    )
    matched_count = sum(
        1
        for entry in match_entries
        if isinstance(entry.get("match"), dict)
        and entry.get("match_status") != "not_llm_ranked"
        and entry.get("source") != "matching_budget_skipped"
    )

    jobs_by_id = job_lookup(jobs)
    bucketed: dict[str, list[tuple[JsonDict, JsonDict]]] = {
        "must_apply": [],
        "strong_maybe": [],
        "maybe": [],
        "weak_fit": [],
        "skip": [],
    }
    hard_filtered: list[tuple[JsonDict, JsonDict]] = []
    low_priority: list[tuple[JsonDict, JsonDict]] = []

    for entry in match_entries:
        job = jobs_by_id.get(as_string(entry.get("job_id")))
        if not job:
            continue
        if entry.get("match_status") == "not_llm_ranked" or entry.get("source") == "matching_budget_skipped":
            low_priority.append((job, entry))
            continue
        hard_filter = entry.get("hard_filter") if isinstance(entry.get("hard_filter"), dict) else {}
        if hard_filter.get("rejected"):
            hard_filtered.append((job, entry))
            continue
        match = entry.get("match") if isinstance(entry.get("match"), dict) else {}
        bucket = as_string(match.get("bucket"), "skip")
        if bucket not in VALID_BUCKETS:
            bucket = "skip"
        bucketed.setdefault(bucket, []).append((job, entry))

    for bucket_jobs in bucketed.values():
        bucket_jobs.sort(
            key=lambda pair: pair[1].get("match", {}).get("score", 0)
            if isinstance(pair[1].get("match"), dict)
            else 0,
            reverse=True,
        )

    skipped = hard_filtered + bucketed.get("skip", [])
    not_llm_ranked_count = len(low_priority)
    heuristic_eligible_count = sum(len(bucketed[bucket]) for bucket in VALID_BUCKETS) + not_llm_ranked_count
    ranked_heuristic_scores = [
        score
        for bucket in VALID_BUCKETS
        for _, entry in bucketed[bucket]
        if (score := as_number_or_none((entry.get("heuristic") or {}).get("score") if isinstance(entry.get("heuristic"), dict) else None)) is not None
    ]
    not_ranked_heuristic_scores = [
        score
        for _, entry in low_priority
        if (score := as_number_or_none((entry.get("heuristic") or {}).get("score") if isinstance(entry.get("heuristic"), dict) else None)) is not None
    ]
    min_ranked_heuristic = min(ranked_heuristic_scores) if ranked_heuristic_scores else None
    max_not_ranked_heuristic = max(not_ranked_heuristic_scores) if not_ranked_heuristic_scores else None
    selection_warning = (
        max_not_ranked_heuristic is not None
        and min_ranked_heuristic is not None
        and max_not_ranked_heuristic > min_ranked_heuristic
    )
    low_priority.sort(
        key=lambda pair: as_number_or_none(pair[1].get("heuristic", {}).get("score"))
        if isinstance(pair[1].get("heuristic"), dict)
        else 0,
        reverse=True,
    )

    lines = [
        f"# HN Hiring Scout Report - {month}",
        "",
        f"Generated: {utc_now_iso()}",
        "",
        "## Thread",
        "",
        f"- Title: {value_or_unknown(thread.get('title'))}",
        f"- URL: {value_or_unknown(thread.get('url'))}",
        f"- Total comments: {total_comments}",
        f"- Comments analyzed: {comments_considered}",
    ]

    if prefilter_counts:
        lines.extend(
            [
                f"- Prefilter keep: {prefilter_counts.get('keep', 0)}",
                f"- Prefilter deprioritize: {prefilter_counts.get('deprioritize', 0)}",
                f"- Prefilter reject: {prefilter_counts.get('reject', 0)}",
            ]
        )

    lines.extend(
        [
            "",
            "## Summary",
            "",
            f"- Total comments: {total_comments}",
            f"- Prefilter keep/deprioritize/reject: {prefilter_counts.get('keep', 0) if prefilter_counts else 0} / {prefilter_counts.get('deprioritize', 0) if prefilter_counts else 0} / {prefilter_counts.get('reject', 0) if prefilter_counts else 0}",
            f"- Extraction LLM calls made/reused: {extraction_llm_calls_made} / {extraction_cache_reused}",
            f"- Roles extracted: {len(jobs)}",
            f"- Hard-filtered roles: {hard_filtered_count}",
            f"- Heuristic eligible roles: {heuristic_eligible_count}",
            f"- LLM matched roles: {matched_count}",
            f"- Not LLM-ranked roles: {not_llm_ranked_count}",
            f"- top_n_for_matching: {top_n_for_matching if top_n_for_matching is not None else 'not set'}",
            f"- Min heuristic score among LLM-ranked jobs: {format_optional_number(min_ranked_heuristic)}",
            f"- Max heuristic score among not-LLM-ranked jobs: {format_optional_number(max_not_ranked_heuristic)}",
            f"- Must apply / strong maybe / maybe: {len(bucketed['must_apply'])} / {len(bucketed['strong_maybe'])} / {len(bucketed['maybe'])}",
            "",
            "## Processing Details",
            "",
            f"- Extracted job posts: {extracted_job_posts}",
            f"- Extracted roles: {len(jobs)}",
            f"- Hard-filtered count: {hard_filtered_count}",
            f"- Matched count: {matched_count}",
            f"- Report mode: {report_mode}",
            "",
        ]
    )

    if dry_run:
        lines.extend(
            [
                "## Dry-Run Warning",
                "",
                "This report was generated in `--dry-run` mode. Extraction and matching are simple keyword heuristics only; no LLM was called and this is not real AI analysis.",
                "",
            ]
        )

    if selection_warning:
        lines.extend(
            [
                "## Selection Warning",
                "",
                "A not-LLM-ranked job has a higher heuristic score than at least one LLM-ranked job. This suggests a selection or cache consistency problem; rerun with `--force-match` after inspecting the match cache.",
                "",
            ]
        )

    detailed_sections = [
        ("Must Apply", bucketed["must_apply"]),
        ("Strong Maybe", bucketed["strong_maybe"]),
        ("Maybe", bucketed["maybe"]),
    ]
    if report_mode == "full":
        detailed_sections.extend(
            [
                ("Weak Fit", bucketed["weak_fit"]),
                ("Hard Filtered / Skipped", skipped),
            ]
        )

    for section_title, pairs in detailed_sections:
        lines.extend([f"## {section_title}", ""])
        if not pairs:
            lines.extend(["No jobs in this section.", ""])
            continue
        for job, entry in pairs:
            lines.extend([rendered_job_details(job, entry), ""])

    if report_mode == "shortlist":
        lines.extend(["## Weak Fit", "", compact_weak_fit_table(bucketed["weak_fit"]), ""])
        lines.extend(["## Hard Filtered / Skipped", "", compact_hard_filtered_table(skipped), ""])

    lines.extend(["## Not LLM-ranked / Low heuristic priority", ""])
    if not low_priority:
        lines.extend(["No jobs in this section.", ""])
    elif report_mode == "shortlist":
        lines.extend([compact_not_llm_ranked_table(low_priority), ""])
    else:
        for job, entry in low_priority:
            lines.extend([rendered_low_priority_job(job, entry), ""])

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def raw_comments(raw_data: JsonDict, *, limit: int | None) -> list[JsonDict]:
    comments = raw_data.get("comments")
    if not isinstance(comments, list):
        raise AnalyzeError("Raw JSON is missing a 'comments' list.")
    normalized = [comment for comment in comments if isinstance(comment, dict)]
    if limit is not None:
        if limit <= 0:
            raise AnalyzeError("--limit must be greater than 0.")
        normalized = normalized[:limit]
    return normalized


def run(args: argparse.Namespace) -> None:
    validate_month(args.month)
    ensure_dependencies(dry_run=args.dry_run or args.prefilter_only)

    raw_path = args.raw_file or default_raw_path(args.month)
    raw_data = read_json_file(raw_path)
    comments = raw_comments(raw_data, limit=args.limit)
    profile = load_profile(args.profile)
    profile_hash = stable_hash(profile)

    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)

    if args.clear_analysis:
        removed = clear_analysis_outputs(args.month)
        if removed:
            print("Cleared analysis outputs:")
            for path in removed:
                print(f"- {path}")
        else:
            print("No existing analysis outputs to clear.")

    prefilter_file = prefilter_cache_path(args.month)
    extract_cache_file = extraction_cache_path(args.month)
    match_cache_file = match_cache_path(args.month)
    report_file = report_path(args.month)

    prefilter_results = prefilter_comments(comments, profile)
    prefilter_counts = comment_prefilter_counts(prefilter_results)
    extraction_comments = extraction_comments_from_prefilter(
        comments,
        prefilter_results,
        include_deprioritized=args.include_deprioritized,
        max_extractions=args.max_extractions,
    )
    write_prefilter_results(prefilter_file, month=args.month, results=prefilter_results)

    extract_cache = load_cache(
        extract_cache_file,
        version=EXTRACTION_CACHE_VERSION,
        month=args.month,
    )
    cached_extractions, new_extractions = count_reusable_extractions(
        extraction_comments,
        extract_cache,
        dry_run=args.dry_run,
        force=args.force_extract,
    )
    new_extraction_llm_calls = 0 if args.dry_run else new_extractions
    estimated_new_matching_calls = 0 if args.dry_run else (
        args.top_n_for_matching if args.top_n_for_matching is not None else len(extraction_comments)
    )
    estimated_total_new_llm_calls = new_extraction_llm_calls + estimated_new_matching_calls

    print_budget_summary(
        "LLM budget summary before extraction",
        [
            ("raw comments total", len(comments)),
            ("prefilter keep count", prefilter_counts.get("keep", 0)),
            ("prefilter deprioritize count", prefilter_counts.get("deprioritize", 0)),
            ("prefilter reject count", prefilter_counts.get("reject", 0)),
            ("extraction candidates", len(extraction_comments)),
            ("cached extractions reused", cached_extractions),
            ("new extraction LLM calls needed", new_extraction_llm_calls),
            ("roles after extraction", "pending extraction"),
            ("roles hard-filtered", "pending extraction"),
            ("eligible jobs after hard filters", "pending extraction"),
            ("top-n for LLM matching", args.top_n_for_matching or "not set"),
            ("jobs skipped from LLM due to heuristic priority", "pending extraction"),
            ("cached matches reused", "pending extraction"),
            (
                "new matching LLM calls needed",
                f"pending extraction; estimated up to {estimated_new_matching_calls}",
            ),
        ],
    )

    if args.prefilter_only:
        print(f"Prefilter results: {prefilter_file}")
        return

    llm_budget_confirmed = args.yes
    if estimated_total_new_llm_calls > 50:
        llm_budget_confirmed = confirm_llm_calls(estimated_total_new_llm_calls, yes=args.yes)
        if not llm_budget_confirmed:
            print("Exiting without making LLM calls.")
            print(f"Prefilter results: {prefilter_file}")
            return

    client, model = make_client(dry_run=args.dry_run)
    if not args.dry_run:
        print(f"LLM configuration: model={model}; base_url={configured_llm_base_url(client)}")
    comments_by_id = {comment.get("id"): comment for comment in extraction_comments}
    extraction_entries = extract_comments(
        comments=extraction_comments,
        cache=extract_cache,
        client=client,
        model=model,
        dry_run=args.dry_run,
        force=args.force_extract,
    )
    write_json_file(extract_cache_file, extract_cache)

    jobs = flatten_jobs(extraction_entries=extraction_entries, comments_by_id=comments_by_id)

    match_cache = load_cache(
        match_cache_file,
        version=MATCH_CACHE_VERSION,
        month=args.month,
    )
    matching_plan = plan_matching_jobs(
        jobs=jobs,
        profile=profile,
        profile_hash=profile_hash,
        cache=match_cache,
        dry_run=args.dry_run,
        force=args.force_match,
        top_n_for_matching=args.top_n_for_matching,
    )
    selected_jobs = matching_plan["selected_jobs"]
    skipped_new_match_jobs = matching_plan["skipped_new_match_jobs"]
    new_matching_llm_calls = 0 if args.dry_run else len(matching_plan["new_match_jobs"])

    print_budget_summary(
        "LLM budget summary before matching",
        [
            ("raw comments total", len(comments)),
            ("prefilter keep count", prefilter_counts.get("keep", 0)),
            ("prefilter deprioritize count", prefilter_counts.get("deprioritize", 0)),
            ("prefilter reject count", prefilter_counts.get("reject", 0)),
            ("extraction candidates", len(extraction_comments)),
            ("cached extractions reused", cached_extractions),
            ("new extraction LLM calls needed", new_extraction_llm_calls),
            ("roles after extraction", len(jobs)),
            ("roles hard-filtered", len(matching_plan["hard_filtered_jobs"])),
            ("eligible jobs after hard filters", len(matching_plan["eligible_jobs"])),
            ("top-n for LLM matching", args.top_n_for_matching or "not set"),
            ("jobs skipped from LLM due to heuristic priority", len(skipped_new_match_jobs)),
            ("cached matches reused", len(matching_plan["cached_match_jobs"])),
            ("new matching LLM calls needed", new_matching_llm_calls),
        ],
    )

    print_matching_selection_debug(
        selected_jobs=matching_plan["selected_eligible_jobs"],
        skipped_jobs=skipped_new_match_jobs,
    )
    if args.show_heuristic_details:
        print_heuristic_details(matching_plan["eligible_jobs"])
    warning = selection_inconsistency_warning(
        matching_plan["selected_eligible_jobs"],
        skipped_new_match_jobs,
    )
    if warning:
        print(warning)

    if new_matching_llm_calls > 50 and not llm_budget_confirmed:
        if not confirm_llm_calls(new_matching_llm_calls, yes=args.yes):
            print("Exiting before LLM matching calls.")
            print(f"Prefilter results: {prefilter_file}")
            print(f"Extraction cache: {extract_cache_file}")
            return

    match_entries = match_jobs(
        jobs=selected_jobs,
        profile=profile,
        profile_hash=profile_hash,
        cache=match_cache,
        client=client,
        model=model,
        dry_run=args.dry_run,
        force=args.force_match,
    )
    if skipped_new_match_jobs:
        skipped_entries = budget_skipped_match_entries(
            skipped_new_match_jobs,
            profile_hash=profile_hash,
            profile=profile,
        )
        cache_items = match_cache.setdefault("items", {})
        if isinstance(cache_items, dict):
            for entry in skipped_entries:
                cache_items[as_string(entry.get("job_id"))] = entry
        match_entries.extend(skipped_entries)
    if args.generate_outreach:
        generate_outreach_drafts(
            match_entries=match_entries,
            jobs=jobs,
            profile=profile,
            client=client,
            model=model,
            dry_run=args.dry_run,
        )
    write_json_file(match_cache_file, match_cache)

    generate_report(
        month=args.month,
        raw_data=raw_data,
        comments_considered=len(extraction_comments),
        prefilter_counts=prefilter_counts,
        extraction_llm_calls_made=new_extraction_llm_calls,
        extraction_cache_reused=cached_extractions,
        extraction_entries=extraction_entries,
        jobs=jobs,
        match_entries=match_entries,
        output_path=report_file,
        top_n_for_matching=args.top_n_for_matching,
        report_mode=args.report_mode,
        dry_run=args.dry_run,
    )

    print(f"Extracted roles: {len(jobs)}")
    print(f"Prefilter results: {prefilter_file}")
    print(f"Extraction cache: {extract_cache_file}")
    print(f"Match cache: {match_cache_file}")
    print(f"Report: {report_file}")


def self_test_profile() -> JsonDict:
    return {
        "candidate": {
            "country": "India",
            "region": "Karnataka",
            "timezone": "Asia/Kolkata",
            "can_work_in_us": False,
            "can_work_in_eu": False,
            "can_work_in_uk": False,
            "requires_visa_sponsorship": True,
            "eligible_remote_regions": ["Worldwide", "Anywhere", "Global"],
            "ineligible_remote_regions": [
                "US only",
                "Remote US",
                "Remote (US)",
                "EU only",
                "UK only",
                "Canada only",
            ],
        },
        "hard_filters": {
            "reject_internships": True,
            "reject_junior_roles": True,
            "reject_onsite_only": True,
            "reject_hybrid_more_than_days_per_week": 1,
            "reject_unpaid": True,
            "reject_equity_only": True,
            "reject_frontend_only": True,
            "reject_mobile_only": True,
            "reject_non_engineering_roles": True,
            "reject_wrong_geo": True,
            "minimum_salary_if_disclosed": None,
        },
        "work_preferences": {"acceptable_locations": ["Remote", "Worldwide remote"]},
        "avoid_roles": ["pure engineering manager"],
    }


def self_test_job(title: str, **role_overrides: Any) -> JsonDict:
    role = {
        "title": title,
        "seniority": [],
        "employment_type": None,
        "location_raw": None,
        "remote_status": "unknown",
        "remote_region": None,
        "salary_raw": None,
        "salary_min": None,
        "salary_max": None,
        "currency": None,
        "equity": None,
        "tech_stack": [],
        "requirements": [],
        "nice_to_haves": [],
        "apply_email": None,
        "apply_url": None,
        "evidence_quotes": [],
    }
    role.update(role_overrides)
    return {
        "company_name": "ExampleCo",
        "company_url": None,
        "company_description": None,
        "role": role,
    }


def self_test_comment(text: str) -> JsonDict:
    return {
        "id": 1,
        "plain_text": text,
        "hn_url": "https://news.ycombinator.com/item?id=1",
        "position_index": 0,
    }


def print_self_test_result(name: str, passed: bool, detail: str = "") -> None:
    status = "PASS" if passed else "FAIL"
    suffix = f" - {detail}" if detail else ""
    print(f"{status} {name}{suffix}")


def run_self_test() -> int:
    tests: list[tuple[str, bool, str]] = []

    parser_cases = [
        ("parse pure JSON", '{"ok": true}', {"ok": True}),
        ("parse fenced JSON", '```json\n{"ok": 2}\n```', {"ok": 2}),
        ("parse text-wrapped JSON", 'before {"ok": 3} after', {"ok": 3}),
    ]
    for name, text, expected in parser_cases:
        try:
            actual = parse_llm_json(text)
            tests.append((name, actual == expected, f"got {actual!r}"))
        except Exception as exc:
            tests.append((name, False, str(exc)))

    profile = self_test_profile()
    salary_cases = [
        (
            "salary duckduckgo disclosed",
            self_test_job("Backend Engineer", salary_raw="$178,500 USD + equity"),
            True,
        ),
        (
            "salary peregrine disclosed",
            self_test_job("Backend Engineer", salary_raw="$175,000 - $300,000"),
            True,
        ),
        (
            "salary k range disclosed",
            self_test_job("Backend Engineer", salary_raw="$135-$155k"),
            True,
        ),
        (
            "salary hourly disclosed",
            self_test_job("Backend Engineer", salary_raw="€20 - €40/hr + Equity"),
            True,
        ),
        (
            "salary not disclosed false",
            self_test_job("Backend Engineer", salary_raw="Not disclosed"),
            False,
        ),
    ]
    for name, job, expected in salary_cases:
        actual = has_salary_disclosed(job)
        tests.append((name, actual == expected, f"got {actual!r}"))

    parsed_k_range = parse_salary_numbers("$135-$155k")
    tests.append(
        (
            "salary k range parsed min max",
            parsed_k_range.get("salary_min") == 135000
            and parsed_k_range.get("salary_max") == 155000
            and parsed_k_range.get("currency") == "$",
            f"got {parsed_k_range!r}",
        )
    )
    parsed_hourly = parse_salary_numbers("€20 - €40/hr + Equity")
    tests.append(
        (
            "salary hourly parsed",
            parsed_hourly.get("salary_min") == 20
            and parsed_hourly.get("salary_max") == 40
            and parsed_hourly.get("currency") == "€"
            and parsed_hourly.get("is_hourly") is True,
            f"got {parsed_hourly!r}",
        )
    )
    duck_salary_heuristic = heuristic_score_job(
        self_test_job("Senior Backend Engineer", salary_raw="$178,500 USD + equity"),
        profile,
    )
    tests.append(
        (
            "heuristic disclosed salary avoids no salary negative",
            not any(
                "no salary disclosed" in signal
                for signal in as_string_list(duck_salary_heuristic.get("negative_signals"))
            ),
            f"got {duck_salary_heuristic!r}",
        )
    )
    mobile_cases = [
        ("mobile react native app true", "React Native Mobile App", True),
        ("mobile senior react native developer true", "Senior React Native Developer", True),
        ("mobile carrier data false", "mobile carrier data", False),
        ("mobile network analytics false", "mobile network analytics", False),
        ("mobile ios engineer true", "iOS Engineer", True),
    ]
    for name, text, expected in mobile_cases:
        actual = is_mobile_engineering_role(text)
        tests.append((name, actual == expected, f"got {actual!r}"))

    carrier_data_heuristic = heuristic_score_job(
        self_test_job(
            "Senior Backend Engineer",
            evidence_quotes=["We work with mobile carrier data and network analytics."],
        ),
        profile,
    )
    tests.append(
        (
            "heuristic mobile carrier data not mobile-heavy",
            not any(
                "mobile-only" in signal.lower() or "mobile-heavy" in signal.lower()
                for signal in as_string_list(carrier_data_heuristic.get("negative_signals"))
            ),
            f"got {carrier_data_heuristic!r}",
        )
    )

    backend_platform_target_cases = [
        (
            "candidate targets backend platform from list",
            {"target_roles": ["Senior Backend Engineer", "Platform Engineer"]},
            True,
        ),
        (
            "candidate frontend target does not imply backend platform",
            {"target_roles": ["Frontend Engineer"]},
            False,
        ),
        (
            "candidate frontend target with backend preference true",
            {
                "target_roles": ["Frontend Engineer"],
                "role_preferences": {"prefer": ["backend", "platform"]},
            },
            True,
        ),
        (
            "candidate targets backend platform from string",
            {"target_roles": "Staff Product Engineering"},
            True,
        ),
        (
            "candidate missing target roles false",
            {},
            False,
        ),
    ]
    for name, test_profile, expected in backend_platform_target_cases:
        actual = candidate_targets_backend_platform(test_profile)
        tests.append((name, actual == expected, f"got {actual!r}"))

    filter_cases = [
        (
            "junior role rejected",
            self_test_job("Junior Backend Engineer"),
            True,
        ),
        (
            "onsite-only role rejected",
            self_test_job("Backend Engineer", remote_status="onsite"),
            True,
        ),
        (
            "remote senior backend role not rejected",
            self_test_job(
                "Senior Backend Engineer",
                remote_status="remote",
                tech_stack=["Python"],
                evidence_quotes=["Remote senior backend role."],
            ),
            False,
        ),
        (
            "equity-only role rejected",
            self_test_job("Backend Engineer", salary_raw="Equity only, no salary"),
            True,
        ),
        (
            "frontend-only role rejected",
            self_test_job("Frontend Engineer"),
            True,
        ),
        (
            "full-stack role not rejected as frontend-only",
            self_test_job("Full-stack Frontend Engineer"),
            False,
        ),
        (
            "mixed junior to senior role not rejected",
            self_test_job("Junior to Senior Fullstack Engineer", seniority=["junior", "senior", "mixed"]),
            False,
        ),
        (
            "senior software engineer not junior rejected",
            self_test_job("Senior Software Engineer"),
            False,
        ),
        (
            "internship role rejected",
            self_test_job("Backend Engineer", evidence_quotes=["Internship"]),
            True,
        ),
        (
            "onsite with remote option not onsite-only rejected",
            self_test_job(
                "Backend Engineer",
                evidence_quotes=["ONSITE (2x/week onsite) or REMOTE"],
            ),
            False,
        ),
        (
            "onsite or fully remote not rejected",
            self_test_job(
                "Backend Engineer",
                evidence_quotes=["ONSITE or FULLY REMOTE"],
            ),
            False,
        ),
        (
            "required monthly onsite remote role rejected above hybrid max",
            self_test_job(
                "Backend Engineer",
                remote_status="remote",
                evidence_quotes=[
                    "Remote within Europe-compatible timezone, one week per month onsite in Paris"
                ],
            ),
            True,
        ),
        (
            "remote US role rejected by hard filter",
            self_test_job(
                "Backend Engineer",
                remote_status="remote",
                location_raw="Remote (US)",
                evidence_quotes=["Remote (US)"],
            ),
            True,
        ),
        (
            "remote Europe role rejected by hard filter",
            self_test_job(
                "Backend Engineer",
                remote_status="remote",
                location_raw="Remote (Europe)",
                evidence_quotes=["Remote (Europe)"],
            ),
            True,
        ),
        (
            "remote Japan role rejected by hard filter",
            self_test_job(
                "Backend Engineer",
                remote_status="remote",
                location_raw="Tokyo or Remote in Japan",
                evidence_quotes=["Tokyo or Remote in Japan"],
            ),
            True,
        ),
    ]

    for name, job, expected_rejected in filter_cases:
        result = apply_hard_filters(job, profile)
        actual_rejected = bool(result.get("rejected"))
        tests.append(
            (
                name,
                actual_rejected == expected_rejected,
                f"rejected={actual_rejected}, reasons={result.get('reasons')}",
            )
        )

    relevance_cases = [
        ("software engineer title relevant", "Software Engineer", True),
        ("product manager title not relevant", "Product Manager", False),
        ("designer title not relevant", "Designer", False),
        ("seo specialist title not relevant", "SEO Specialist", False),
        ("developer advocate title not relevant", "Developer Advocate", False),
        ("sre title relevant", "SRE / Site Reliability Engineer", True),
    ]
    for name, title, expected in relevance_cases:
        actual = is_relevant_engineering_role_title(title)
        tests.append((name, actual == expected, f"got {actual!r}"))

    onsite_helper_cases = [
        (
            "helper remote option avoids onsite-only",
            "ONSITE (2x/week onsite) or REMOTE",
            False,
        ),
        (
            "helper jersey city onsite-only detected",
            "Flotive AI | Jersey City / New York Metropolitan Area / NYC Area | Onsite",
            True,
        ),
        (
            "helper on-site daily detected",
            "On-site daily in Brooklyn",
            True,
        ),
        (
            "helper onsite or fully remote not onsite-only",
            "ONSITE or FULLY REMOTE",
            False,
        ),
    ]
    for name, text, expected in onsite_helper_cases:
        actual = is_clearly_onsite_only(text)
        tests.append((name, actual == expected, f"got {actual!r}"))

    prefilter_cases = [
        (
            "prefilter onsite plus remote option not rejected",
            "ExampleCo | Backend Engineer | ONSITE (2x/week onsite) or REMOTE",
            False,
        ),
        (
            "prefilter junior first-line rejected",
            "Junior | Remote | Any Time Zone",
            True,
        ),
        (
            "prefilter junior software engineer rejected",
            "Junior Software Engineer | Remote US",
            True,
        ),
        (
            "prefilter internship rejected",
            "Internship",
            True,
        ),
        (
            "prefilter senior software engineer not junior rejected",
            "Senior Software Engineer | Remote | Any Time Zone",
            False,
        ),
        (
            "prefilter jersey city onsite rejected",
            "Flotive AI | Backend Engineer | Jersey City / New York Metropolitan Area / NYC Area | Onsite",
            True,
        ),
        (
            "prefilter loccitane onsite nyc rejected",
            "L'Occitane | Software Engineer | New York, NY | Onsite",
            True,
        ),
        (
            "prefilter onsite or fully remote not rejected",
            "ExampleCo | Backend Engineer | ONSITE or FULLY REMOTE",
            False,
        ),
        (
            "prefilter monthly onsite remote role rejected above hybrid max",
            "ExampleCo | Backend Engineer | Remote within Europe-compatible timezone, one week per month onsite in Paris",
            True,
        ),
    ]
    for name, text, expected_rejected in prefilter_cases:
        result = prefilter_comment(self_test_comment(text), profile)
        actual_rejected = result.get("decision") == "reject"
        tests.append(
            (
                name,
                actual_rejected == expected_rejected,
                f"decision={result.get('decision')}, reasons={result.get('reasons')}",
            )
        )

    mixed_prefilter = prefilter_comment(
        self_test_comment("Junior to Senior Fullstack Engineer | Remote | Any Time Zone"),
        profile,
    )
    tests.append(
        (
            "prefilter mixed junior to senior deprioritized not rejected",
            mixed_prefilter.get("decision") == "deprioritize"
            and "Mixed seniority posting; verify senior-level opening exists."
            in as_string_list(mixed_prefilter.get("reasons")),
            f"decision={mixed_prefilter.get('decision')}, reasons={mixed_prefilter.get('reasons')}",
        )
    )
    senior_and_junior_prefilter = prefilter_comment(
        self_test_comment("Senior & Junior Software Engineers | Remote | Any Time Zone"),
        profile,
    )
    tests.append(
        (
            "prefilter senior and junior mixed deprioritized not rejected",
            senior_and_junior_prefilter.get("decision") == "deprioritize"
            and "Mixed seniority posting; verify senior-level opening exists."
            in as_string_list(senior_and_junior_prefilter.get("reasons")),
            f"decision={senior_and_junior_prefilter.get('decision')}, reasons={senior_and_junior_prefilter.get('reasons')}",
        )
    )

    geo_cases = [
        ("geo remote global compatible", "Remote (Global)", True),
        ("geo remote worldwide compatible", "REMOTE (worldwide)", True),
        ("geo remote US incompatible", "Remote (US)", False),
        ("geo remote Europe incompatible", "Remote (Europe)", False),
        ("geo USA remote incompatible", "USA (Remote)", False),
        ("geo remote Japan incompatible", "Tokyo or Remote in Japan", False),
        (
            "geo poland romania incompatible",
            "REMOTE, must be based in Poland or Romania",
            False,
        ),
        ("geo oregon usa only incompatible", "Remote okay. Oregon USA only.", False),
        ("geo latam remote incompatible", "LATAM Remote", False),
        ("geo remote americas incompatible", "Remote (Americas)", False),
        ("geo remote apac compatible", "Remote APAC", True),
        ("geo remote emea apac compatible", "REMOTE (EMEA/APAC)", True),
        ("geo remote india compatible", "Remote India", True),
        ("geo remote singapore incompatible", "Remote Singapore", False),
    ]
    for name, text, expected_compatible in geo_cases:
        restrictions = detect_geo_restrictions(text)
        actual_compatible, reason = is_geo_compatible(restrictions, profile)
        tests.append(
            (
                name,
                actual_compatible == expected_compatible,
                f"compatible={actual_compatible}, restrictions={restrictions}, reason={reason}",
            )
        )

    timezone_cases = [
        ("timezone asia kolkata offset", candidate_timezone_offset(profile), 5.5),
        (
            "timezone utc range incompatible",
            timezone_compatible("UTC -8 - UTC +2", profile)[0],
            False,
        ),
        (
            "timezone remote apac compatible no penalty",
            timezone_compatible("Remote APAC", profile)[0],
            True,
        ),
        (
            "timezone cet window incompatible",
            timezone_compatible("EU CET +-2", profile)[0],
            False,
        ),
    ]
    for name, actual, expected in timezone_cases:
        tests.append((name, actual == expected, f"got {actual!r}"))

    timezone_prefilter = prefilter_comment(
        self_test_comment("ExampleCo | Senior Backend Engineer | Remote | UTC -8 - UTC +2"),
        profile,
    )
    tests.append(
        (
            "prefilter incompatible timezone deprioritized",
            timezone_prefilter.get("decision") == "deprioritize"
            and "Timezone window appears incompatible with candidate timezone."
            in as_string_list(timezone_prefilter.get("reasons")),
            f"decision={timezone_prefilter.get('decision')}, reasons={timezone_prefilter.get('reasons')}",
        )
    )
    timezone_heuristic = heuristic_score_job(
        self_test_job(
            "Senior Backend Engineer",
            remote_status="remote",
            evidence_quotes=["UTC -8 - UTC +2"],
        ),
        profile,
    )
    tests.append(
        (
            "heuristic incompatible timezone penalty",
            any(
                "timezone window incompatible" in signal
                for signal in as_string_list(timezone_heuristic.get("negative_signals"))
            )
            and (as_number_or_none(timezone_heuristic.get("score")) or 0) <= 75,
            f"got {timezone_heuristic!r}",
        )
    )

    geo_prefilter_cases = [
        (
            "prefilter remote US rejected",
            "ExampleCo | Backend Engineer | Remote (US)",
            True,
        ),
        (
            "prefilter remote Europe rejected",
            "ExampleCo | Backend Engineer | Remote (Europe)",
            True,
        ),
        (
            "prefilter USA remote rejected",
            "ExampleCo | Backend Engineer | USA (Remote)",
            True,
        ),
        (
            "prefilter japan remote rejected",
            "ExampleCo | Backend Engineer | Tokyo or Remote in Japan",
            True,
        ),
        (
            "prefilter poland romania rejected",
            "ExampleCo | Backend Engineer | REMOTE, must be based in Poland or Romania",
            True,
        ),
        (
            "prefilter oregon usa only rejected",
            "ExampleCo | Backend Engineer | Remote okay. Oregon USA only.",
            True,
        ),
    ]
    for name, text, expected_rejected in geo_prefilter_cases:
        result = prefilter_comment(self_test_comment(text), profile)
        actual_rejected = result.get("decision") == "reject"
        tests.append(
            (
                name,
                actual_rejected == expected_rejected,
                f"decision={result.get('decision')}, reasons={result.get('reasons')}",
            )
        )

    for name, passed, detail in tests:
        print_self_test_result(name, passed, "" if passed else detail)

    passed_count = sum(1 for _, passed, _ in tests if passed)
    print(f"{passed_count}/{len(tests)} self-tests passed.")
    return 0 if passed_count == len(tests) else 1


def main() -> int:
    args = parse_args()
    if args.self_test:
        return run_self_test()

    try:
        run(args)
    except AnalyzeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
