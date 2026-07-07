#!/usr/bin/env python3
"""Generate an HN Hiring Scout profile YAML from candidate documents."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from dataclasses import dataclass
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
    import yaml
except ImportError:  # pragma: no cover - exercised only in incomplete environments.
    yaml = None  # type: ignore[assignment]

try:
    from pypdf import PdfReader
except ImportError:  # pragma: no cover - optional until PDF extraction is used.
    PdfReader = None  # type: ignore[assignment,misc]

try:
    import docx
except ImportError:  # pragma: no cover - optional until DOCX extraction is used.
    docx = None  # type: ignore[assignment]


JsonDict = dict[str, Any]

TEXT_EXTENSIONS = {
    ".txt",
    ".md",
    ".markdown",
    ".yaml",
    ".yml",
    ".json",
    ".csv",
    ".tsv",
    ".log",
}
DEFAULT_MAX_CHARS = 120_000
DEFAULT_MAX_FILE_BYTES = 12 * 1024 * 1024
CHUNK_SIZE = 35_000
CHUNK_OVERLAP = 1_000
PROTECTED_PROFILE_NAMES = {"profile.yaml", "profile.gaurav.yaml"}
REQUIRED_SECTIONS = {
    "candidate",
    "skills",
    "work_preferences",
    "compensation",
    "hard_filters",
    "scoring_weights",
    "outreach",
    "feedback_preferences",
}
REQUIRED_CANDIDATE_FIELDS = {
    "country",
    "region",
    "city",
    "timezone",
    "can_work_in_us",
    "can_work_in_eu",
    "can_work_in_uk",
    "requires_visa_sponsorship",
    "eligible_remote_regions",
    "ineligible_remote_regions",
}


class GenerateProfileError(RuntimeError):
    """Raised for user-facing profile generation errors."""


@dataclass(frozen=True)
class ExtractedDocument:
    path: Path
    kind: str
    text: str
    skipped_reason: str | None = None

    @property
    def char_count(self) -> int:
        return len(self.text)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate a validated HN Hiring Scout profile YAML from candidate documents."
    )
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="Run local validation and extraction self-tests, then exit.",
    )
    parser.add_argument(
        "--input",
        action="append",
        type=Path,
        dest="inputs",
        help="Candidate file or directory. Can be passed more than once.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Output profile YAML path, for example profile.generated.yaml.",
    )
    parser.add_argument(
        "--template",
        type=Path,
        default=Path("profile.yaml"),
        help="Profile template/schema source. Defaults to profile.yaml.",
    )
    parser.add_argument(
        "--max-chars",
        type=int,
        default=DEFAULT_MAX_CHARS,
        help=f"Maximum evidence characters for the final profile call. Default: {DEFAULT_MAX_CHARS}.",
    )
    parser.add_argument(
        "--max-file-bytes",
        type=int,
        default=DEFAULT_MAX_FILE_BYTES,
        help=f"Skip files larger than this many bytes. Default: {DEFAULT_MAX_FILE_BYTES}.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite the output file if it already exists.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Discover and extract input text, but do not call the LLM or write output.",
    )
    parser.add_argument(
        "--include-private-links",
        action="store_true",
        help="Allow the LLM to populate personal links found in the documents.",
    )
    parser.add_argument(
        "--include-evidence-section",
        action="store_true",
        help="Include an evidence_profile section with short source-backed notes.",
    )
    args = parser.parse_args()

    if not args.self_test:
        if not args.inputs:
            parser.error("at least one --input file or directory is required unless --self-test is used.")
        if not args.output and not args.dry_run:
            parser.error("--output is required unless --dry-run or --self-test is used.")
        if args.max_chars <= 5_000:
            parser.error("--max-chars must be greater than 5000.")
        if args.max_file_bytes <= 0:
            parser.error("--max-file-bytes must be greater than 0.")
    return args


def ensure_dependencies(*, dry_run: bool) -> None:
    missing: list[str] = []
    if load_dotenv is None:
        missing.append("python-dotenv")
    if yaml is None:
        missing.append("PyYAML")
    if not dry_run and OpenAI is None:
        missing.append("openai")
    if missing:
        raise GenerateProfileError(
            "Missing required package(s): "
            + ", ".join(missing)
            + ". Install dependencies with: python3 -m pip install -r requirements.txt"
        )


def make_client(*, dry_run: bool) -> tuple[Any | None, str | None]:
    if load_dotenv is not None:
        load_dotenv()
    if dry_run:
        return None, None

    api_key = os.getenv("LLM_API_KEY")
    model = os.getenv("LLM_MODEL")
    base_url = os.getenv("LLM_BASE_URL") or None

    if not api_key:
        raise GenerateProfileError("LLM_API_KEY is required unless --dry-run is used.")
    if not model:
        raise GenerateProfileError("LLM_MODEL is required unless --dry-run is used.")
    if OpenAI is None:
        raise GenerateProfileError("Missing required package: openai.")

    if base_url:
        return OpenAI(api_key=api_key, base_url=base_url), model
    return OpenAI(api_key=api_key), model


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
    parse_errors: list[str] = []
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        parse_errors.append(f"raw JSON parse failed: {exc}")
    else:
        if isinstance(parsed, dict):
            return parsed
        raise ValueError(f"LLM response JSON must be an object. Preview: {preview_text(text)}")

    unfenced = strip_markdown_json_fence(text)
    if unfenced != text.strip():
        try:
            parsed = json.loads(unfenced)
        except json.JSONDecodeError as exc:
            parse_errors.append(f"markdown-fence JSON parse failed: {exc}")
        else:
            if isinstance(parsed, dict):
                return parsed
            raise ValueError(f"LLM response JSON must be an object. Preview: {preview_text(text)}")

    candidate = largest_brace_json_candidate(unfenced)
    if candidate is not None:
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError as exc:
            parse_errors.append(f"largest-brace JSON parse failed: {exc}")
        else:
            if isinstance(parsed, dict):
                return parsed
            raise ValueError(f"LLM response JSON must be an object. Preview: {preview_text(text)}")

    detail = "; ".join(parse_errors[-2:]) if parse_errors else "no JSON object found"
    raise ValueError(
        f"Could not parse LLM response as JSON ({detail}). Preview: {preview_text(text)}"
    )


def chat_json(
    *,
    client: Any,
    model: str,
    system_prompt: str,
    user_prompt: str,
) -> JsonDict:
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
    except Exception:
        response = client.chat.completions.create(**kwargs)

    content = response.choices[0].message.content
    if not isinstance(content, str) or not content.strip():
        raise GenerateProfileError("LLM returned an empty response.")
    try:
        return parse_llm_json(content)
    except ValueError as exc:
        raise GenerateProfileError(str(exc)) from exc


def load_yaml_file(path: Path) -> JsonDict:
    if yaml is None:
        raise GenerateProfileError("Missing required package: PyYAML.")
    if not path.exists():
        raise GenerateProfileError(f"Template file does not exist: {path}")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:  # type: ignore[union-attr]
        raise GenerateProfileError(f"Invalid YAML in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise GenerateProfileError(f"Expected top-level YAML mapping in {path}.")
    return data


def discover_input_files(inputs: list[Path], *, max_file_bytes: int) -> list[Path]:
    files: list[Path] = []
    for input_path in inputs:
        expanded = input_path.expanduser()
        if not expanded.exists():
            raise GenerateProfileError(f"Input path does not exist: {input_path}")
        if expanded.is_dir():
            for path in sorted(expanded.rglob("*")):
                if should_skip_path(path):
                    continue
                if path.is_file() and file_size_ok(path, max_file_bytes=max_file_bytes):
                    files.append(path)
        elif expanded.is_file():
            if should_skip_path(expanded):
                continue
            if file_size_ok(expanded, max_file_bytes=max_file_bytes):
                files.append(expanded)
        else:
            raise GenerateProfileError(f"Input path is not a regular file or directory: {input_path}")
    return dedupe_paths(files)


def should_skip_path(path: Path) -> bool:
    return any(part.startswith(".") for part in path.parts if part not in {".", ".."})


def file_size_ok(path: Path, *, max_file_bytes: int) -> bool:
    try:
        return path.stat().st_size <= max_file_bytes
    except OSError:
        return False


def dedupe_paths(paths: list[Path]) -> list[Path]:
    seen: set[Path] = set()
    result: list[Path] = []
    for path in paths:
        resolved = path.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        result.append(path)
    return result


def extract_documents(paths: list[Path]) -> list[ExtractedDocument]:
    documents: list[ExtractedDocument] = []
    for path in paths:
        documents.append(extract_document(path))
    return documents


def extract_document(path: Path) -> ExtractedDocument:
    suffix = path.suffix.lower()
    try:
        if suffix == ".pdf":
            text = extract_pdf_text(path)
            return ExtractedDocument(path=path, kind="pdf", text=clean_extracted_text(text))
        if suffix == ".docx":
            text = extract_docx_text(path)
            return ExtractedDocument(path=path, kind="docx", text=clean_extracted_text(text))
        if suffix in TEXT_EXTENSIONS:
            text = extract_text_file(path)
            return ExtractedDocument(path=path, kind="text", text=clean_extracted_text(text))
        text = extract_text_file(path)
        if not looks_like_text(text):
            return ExtractedDocument(
                path=path,
                kind="unknown",
                text="",
                skipped_reason="unknown extension did not decode as useful text",
            )
        return ExtractedDocument(path=path, kind="text", text=clean_extracted_text(text))
    except GenerateProfileError as exc:
        return ExtractedDocument(path=path, kind=suffix.lstrip(".") or "unknown", text="", skipped_reason=str(exc))


def extract_pdf_text(path: Path) -> str:
    if PdfReader is None:
        raise GenerateProfileError("pypdf is required to read PDF files.")
    try:
        reader = PdfReader(str(path))
        parts = [page.extract_text() or "" for page in reader.pages]
    except Exception as exc:
        raise GenerateProfileError(f"could not read PDF: {exc}") from exc
    return "\n\n".join(parts)


def extract_docx_text(path: Path) -> str:
    if docx is None:
        raise GenerateProfileError("python-docx is required to read DOCX files.")
    try:
        document = docx.Document(str(path))
        paragraphs = [paragraph.text for paragraph in document.paragraphs]
        table_cells: list[str] = []
        for table in document.tables:
            for row in table.rows:
                table_cells.extend(cell.text for cell in row.cells)
    except Exception as exc:
        raise GenerateProfileError(f"could not read DOCX: {exc}") from exc
    return "\n".join(paragraphs + table_cells)


def extract_text_file(path: Path) -> str:
    data = path.read_bytes()
    for encoding in ("utf-8", "utf-8-sig", "latin-1"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise GenerateProfileError("could not decode as text")


def looks_like_text(text: str) -> bool:
    if not text.strip():
        return False
    sample = text[:4000]
    if "\x00" in sample:
        return False
    printable = sum(1 for char in sample if char.isprintable() or char.isspace())
    return printable / max(len(sample), 1) > 0.85


def clean_extracted_text(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{4,}", "\n\n\n", text)
    return text.strip()


def print_document_summary(documents: list[ExtractedDocument]) -> None:
    total_chars = sum(document.char_count for document in documents)
    usable = [document for document in documents if document.text.strip()]
    print(f"Discovered documents: {len(documents)}")
    print(f"Usable documents: {len(usable)}")
    print(f"Total extracted characters: {total_chars}")
    for document in documents:
        status = f"{document.char_count} chars"
        if document.skipped_reason:
            status = f"skipped: {document.skipped_reason}"
        print(f"- {document.path} [{document.kind}] {status}")


def build_evidence_text(documents: list[ExtractedDocument]) -> str:
    sections: list[str] = []
    for index, document in enumerate(documents, start=1):
        if not document.text.strip():
            continue
        sections.append(
            "\n".join(
                [
                    f"=== SOURCE {index}: {document.path} ===",
                    f"TYPE: {document.kind}",
                    document.text,
                ]
            )
        )
    return "\n\n".join(sections).strip()


def chunk_text(text: str, *, chunk_size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> list[str]:
    if len(text) <= chunk_size:
        return [text]
    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = min(start + chunk_size, len(text))
        chunks.append(text[start:end])
        if end == len(text):
            break
        start = max(end - overlap, start + 1)
    return chunks


def summarize_large_evidence(*, client: Any, model: str, evidence: str, max_chars: int) -> str:
    if len(evidence) <= max_chars:
        return evidence

    chunks = chunk_text(evidence)
    summaries: list[str] = []
    print(f"Evidence is {len(evidence)} characters; summarizing {len(chunks)} chunks before final profile generation.")
    for index, chunk in enumerate(chunks, start=1):
        print(f"Summarizing evidence chunk {index}/{len(chunks)}...")
        result = chat_json(
            client=client,
            model=model,
            system_prompt=summary_system_prompt(),
            user_prompt=summary_user_prompt(chunk=chunk, index=index, total=len(chunks)),
        )
        summary = result.get("summary")
        if not isinstance(summary, str) or not summary.strip():
            raise GenerateProfileError(f"LLM summary for chunk {index} did not include a non-empty summary.")
        summaries.append(summary.strip())

    combined = "\n\n".join(f"=== SUMMARY {i} ===\n{summary}" for i, summary in enumerate(summaries, start=1))
    if len(combined) > max_chars:
        return combined[:max_chars]
    return combined


def summary_system_prompt() -> str:
    return (
        "You summarize candidate career evidence for a profile generator. "
        "Use only the supplied text. Preserve concrete skills, domains, role scope, achievements, "
        "locations, work authorization statements, links, compensation hints, and job preferences. "
        "Do not invent facts. Return JSON only."
    )


def summary_user_prompt(*, chunk: str, index: int, total: int) -> str:
    return (
        f"Summarize evidence chunk {index} of {total} for generating an HN Hiring Scout profile.\n"
        "Return JSON with exactly this shape:\n"
        '{ "summary": "..." }\n\n'
        "Evidence chunk:\n"
        f"{chunk}"
    )


def profile_system_prompt() -> str:
    return (
        "You generate profile YAML data for HN Hiring Scout, a local job-search tool. "
        "Use only the evidence supplied by the user and the template schema. "
        "Do not hallucinate personal links, work authorization, dates, employers, compensation, or credentials. "
        "Use placeholders such as EDIT_ME or null when evidence is missing. "
        "Return valid JSON only."
    )


def profile_user_prompt(
    *,
    template: JsonDict,
    evidence: str,
    include_private_links: bool,
    include_evidence_section: bool,
) -> str:
    evidence_instruction = (
        "Include an evidence_profile top-level section with concise source-backed notes."
        if include_evidence_section
        else "Do not add top-level sections beyond the template schema."
    )
    link_instruction = (
        "You may populate personal links only when they are explicitly present in the evidence."
        if include_private_links
        else "Do not populate personal links; keep link values as placeholders or null unless the template already has placeholders."
    )
    return (
        "Generate a candidate profile JSON object that matches this YAML template schema exactly.\n\n"
        "Rules:\n"
        "- Preserve every top-level key from the template.\n"
        "- Preserve nested dictionary/list shapes from the template.\n"
        "- Fill values using only the evidence below.\n"
        "- Use EDIT_ME or null for missing geography, work authorization, compensation, private links, or preferences.\n"
        "- Make job-search preferences practical and ruthless for senior software engineering roles.\n"
        "- scoring_weights must sum to 100.\n"
        "- candidate geography and work authorization fields must be present and explicit.\n"
        "- Distinguish strongest skills from skills that are merely mentioned.\n"
        f"- {link_instruction}\n"
        f"- {evidence_instruction}\n\n"
        "Template JSON:\n"
        f"{json.dumps(template, indent=2, ensure_ascii=False)}\n\n"
        "Evidence:\n"
        f"{evidence}"
    )


def normalize_profile(profile: JsonDict, template: JsonDict) -> JsonDict:
    normalized = normalize_against_template(profile, template)
    if "scoring_weights" in normalized:
        normalized["scoring_weights"] = normalize_scoring_weights(normalized["scoring_weights"])
    return normalized


def normalize_against_template(value: Any, template: Any) -> Any:
    if isinstance(template, dict):
        source = value if isinstance(value, dict) else {}
        result: JsonDict = {}
        for key, template_value in template.items():
            result[key] = normalize_against_template(source.get(key), template_value)
        for key, source_value in source.items():
            if key not in result:
                result[key] = source_value
        return result
    if isinstance(template, list):
        if isinstance(value, list):
            return value
        if value is None:
            return []
        if isinstance(value, str) and value.strip():
            return [value.strip()]
        return []
    if isinstance(template, bool):
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in {"true", "yes", "1"}
        return bool(value) if value is not None else template
    if isinstance(template, (int, float)) and not isinstance(template, bool):
        if value is None:
            return template
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return value
        if isinstance(value, str):
            try:
                number = float(value.replace(",", "").strip())
            except ValueError:
                return template
            return int(number) if isinstance(template, int) else number
        return template
    return value if value is not None else template


def normalize_scoring_weights(value: Any) -> JsonDict:
    if not isinstance(value, dict):
        return {}
    result: JsonDict = {}
    for key, raw in value.items():
        if isinstance(raw, bool):
            result[key] = 0
        elif isinstance(raw, (int, float)):
            result[key] = int(raw)
        elif isinstance(raw, str):
            try:
                result[key] = int(float(raw.strip()))
            except ValueError:
                result[key] = 0
        else:
            result[key] = 0
    return result


def validate_generated_profile(
    profile: JsonDict,
    template: JsonDict,
    *,
    include_evidence_section: bool,
) -> list[str]:
    errors: list[str] = []
    for key in template:
        if key not in profile:
            errors.append(f"missing top-level key: {key}")

    for section in sorted(REQUIRED_SECTIONS):
        if section not in profile:
            errors.append(f"missing required section: {section}")
        elif not isinstance(profile[section], dict):
            errors.append(f"section must be a mapping: {section}")

    validate_shape(profile, template, path="", errors=errors)
    validate_candidate_geo(profile, errors=errors)
    validate_scoring_weights(profile, errors=errors)

    if "evidence_profile" in profile and not include_evidence_section and "evidence_profile" not in template:
        errors.append("unexpected top-level key: evidence_profile")

    return errors


def validate_shape(value: Any, template: Any, *, path: str, errors: list[str]) -> None:
    if isinstance(template, dict):
        if not isinstance(value, dict):
            errors.append(f"expected mapping at {path or '<root>'}")
            return
        for key, template_value in template.items():
            child_path = f"{path}.{key}" if path else key
            if key not in value:
                errors.append(f"missing key: {child_path}")
                continue
            validate_shape(value[key], template_value, path=child_path, errors=errors)
        return
    if isinstance(template, list):
        if not isinstance(value, list):
            errors.append(f"expected list at {path}")
        return
    if isinstance(template, bool):
        if not isinstance(value, bool):
            errors.append(f"expected boolean at {path}")
        return
    if isinstance(template, (int, float)) and not isinstance(template, bool):
        if value is not None and (not isinstance(value, (int, float)) or isinstance(value, bool)):
            errors.append(f"expected number or null at {path}")


def validate_candidate_geo(profile: JsonDict, *, errors: list[str]) -> None:
    candidate = profile.get("candidate")
    if not isinstance(candidate, dict):
        return
    for field in sorted(REQUIRED_CANDIDATE_FIELDS):
        if field not in candidate:
            errors.append(f"candidate missing geography/work authorization field: {field}")
    for field in ("eligible_remote_regions", "ineligible_remote_regions"):
        if field in candidate and not isinstance(candidate[field], list):
            errors.append(f"candidate.{field} must be a list")
    for field in ("can_work_in_us", "can_work_in_eu", "can_work_in_uk", "requires_visa_sponsorship"):
        if field in candidate and not isinstance(candidate[field], bool):
            errors.append(f"candidate.{field} must be a boolean")


def validate_scoring_weights(profile: JsonDict, *, errors: list[str]) -> None:
    weights = profile.get("scoring_weights")
    if not isinstance(weights, dict):
        return
    total = 0
    for key, value in weights.items():
        if not isinstance(value, int) or isinstance(value, bool):
            errors.append(f"scoring_weights.{key} must be an integer")
            continue
        total += value
    if total != 100:
        errors.append(f"scoring_weights must sum to 100; got {total}")


def generate_profile(
    *,
    client: Any,
    model: str,
    template: JsonDict,
    evidence: str,
    include_private_links: bool,
    include_evidence_section: bool,
) -> JsonDict:
    result = chat_json(
        client=client,
        model=model,
        system_prompt=profile_system_prompt(),
        user_prompt=profile_user_prompt(
            template=template,
            evidence=evidence,
            include_private_links=include_private_links,
            include_evidence_section=include_evidence_section,
        ),
    )
    profile = result.get("profile") if isinstance(result.get("profile"), dict) else result
    if not isinstance(profile, dict):
        raise GenerateProfileError("LLM did not return a profile object.")
    return profile


def write_profile_yaml(path: Path, profile: JsonDict, *, template_path: Path, force: bool) -> None:
    if yaml is None:
        raise GenerateProfileError("Missing required package: PyYAML.")
    if path.exists() and not force:
        raise GenerateProfileError(f"Output file already exists: {path}. Use --force to overwrite.")
    if path.name in PROTECTED_PROFILE_NAMES and path.exists() and not force:
        raise GenerateProfileError(f"Refusing to overwrite {path} without --force.")

    header = (
        "# Generated HN Hiring Scout profile.\n"
        "# Review geography, work authorization, compensation, and hard filters before analysis.\n"
        f"# Template source: {template_path}\n\n"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    body = yaml.safe_dump(  # type: ignore[union-attr]
        profile,
        sort_keys=False,
        allow_unicode=True,
        default_flow_style=False,
        width=100,
    )
    path.write_text(header + body, encoding="utf-8")


def run_self_test() -> int:
    failures: list[str] = []
    template = self_test_template()
    valid_profile = self_test_profile()

    errors = validate_generated_profile(valid_profile, template, include_evidence_section=False)
    if errors:
        failures.append(f"valid profile failed validation: {errors}")

    missing = dict(valid_profile)
    missing.pop("candidate")
    errors = validate_generated_profile(missing, template, include_evidence_section=False)
    if not any("candidate" in error for error in errors):
        failures.append("missing candidate section did not fail validation")

    bad_weights = json.loads(json.dumps(valid_profile))
    bad_weights["scoring_weights"]["skill_match"] = 99
    errors = validate_generated_profile(bad_weights, template, include_evidence_section=False)
    if not any("sum to 100" in error for error in errors):
        failures.append("bad scoring weights did not fail validation")

    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            text_path = tmp / "resume.txt"
            text_path.write_text("Senior Backend Engineer\nPython, APIs, cloud", encoding="utf-8")
            md_path = tmp / "case.md"
            md_path.write_text("# Case Study\nBuilt a marketplace platform.", encoding="utf-8")
            pdf_path = tmp / "resume.pdf"
            pdf_path.write_bytes(b"%PDF-1.4\n% self-test placeholder\n")
            docx_path = tmp / "notes.docx"
            docx_path.write_bytes(b"self-test placeholder")
            binary_path = tmp / "image.bin"
            binary_path.write_bytes(b"\x00\x01\x02\x03")

            docs = extract_documents(discover_input_files([tmp], max_file_bytes=DEFAULT_MAX_FILE_BYTES))
            usable_names = {doc.path.name for doc in docs if doc.text.strip()}
            if "resume.txt" not in usable_names or "case.md" not in usable_names:
                failures.append("text extraction self-test did not include expected files")
            if any(doc.path.name == "image.bin" and doc.text.strip() for doc in docs):
                failures.append("binary-looking unknown file was treated as useful text")
            kinds_by_name = {doc.path.name: doc.kind for doc in docs}
            if kinds_by_name.get("resume.pdf") != "pdf":
                failures.append("PDF file was not routed through PDF extraction")
            if kinds_by_name.get("notes.docx") != "docx":
                failures.append("DOCX file was not routed through DOCX extraction")
    except Exception as exc:
        failures.append(f"extraction self-test failed: {exc}")

    if failures:
        for failure in failures:
            print(f"FAIL: {failure}")
        return 1
    print("PASS: generate_profile.py self-tests passed")
    return 0


def self_test_template() -> JsonDict:
    return {
        "candidate": {
            "name": "YOUR_NAME",
            "country": "EDIT_ME",
            "region": "EDIT_ME",
            "city": "EDIT_ME",
            "timezone": "EDIT_ME",
            "can_work_in_us": False,
            "can_work_in_eu": False,
            "can_work_in_uk": False,
            "requires_visa_sponsorship": True,
            "eligible_remote_regions": [],
            "ineligible_remote_regions": [],
        },
        "target_roles": {"strongly_prefer": [], "open_to": []},
        "avoid_roles": [],
        "skills": {"strongest": [], "comfortable": [], "learning_or_interested": [], "avoid_or_not_primary": []},
        "domains": {"interested": [], "neutral": [], "avoid": []},
        "work_preferences": {"remote": {}, "acceptable_locations": [], "unacceptable_locations": []},
        "compensation": {"minimum_base_usd": None, "desired_base_usd": None},
        "hard_filters": {"reject_onsite_only": True, "minimum_salary_if_disclosed": None},
        "scoring_weights": {
            "role_seniority_match": 20,
            "skill_match": 25,
            "domain_interest": 15,
            "remote_location_fit": 15,
            "compensation_fit": 10,
            "company_stage_fit": 8,
            "post_quality_signal": 7,
        },
        "outreach": {"tone": "", "max_words": 180, "things_to_emphasize": [], "things_to_avoid": []},
        "feedback_preferences": {"common_skip_reasons": {}, "ranking_style": "ruthless"},
    }


def self_test_profile() -> JsonDict:
    profile = json.loads(json.dumps(self_test_template()))
    profile["candidate"]["name"] = "Test Candidate"
    return profile


def main() -> int:
    args = parse_args()
    if args.self_test:
        ensure_dependencies(dry_run=True)
        return run_self_test()

    ensure_dependencies(dry_run=args.dry_run)
    template = load_yaml_file(args.template)
    files = discover_input_files(args.inputs, max_file_bytes=args.max_file_bytes)
    if not files:
        raise GenerateProfileError("No readable input files found.")

    documents = extract_documents(files)
    print_document_summary(documents)
    evidence = build_evidence_text(documents)
    if not evidence:
        raise GenerateProfileError("No usable text could be extracted from the supplied files.")

    if args.dry_run:
        print("Dry run complete. No LLM calls were made and no files were written.")
        return 0

    client, model = make_client(dry_run=False)
    if client is None or model is None:
        raise GenerateProfileError("Could not initialize LLM client.")

    prepared_evidence = summarize_large_evidence(
        client=client,
        model=model,
        evidence=evidence,
        max_chars=args.max_chars,
    )
    print("Generating profile with LLM...")
    raw_profile = generate_profile(
        client=client,
        model=model,
        template=template,
        evidence=prepared_evidence,
        include_private_links=args.include_private_links,
        include_evidence_section=args.include_evidence_section,
    )
    profile = normalize_profile(raw_profile, template)
    errors = validate_generated_profile(
        profile,
        template,
        include_evidence_section=args.include_evidence_section,
    )
    if errors:
        formatted = "\n".join(f"- {error}" for error in errors)
        raise GenerateProfileError(f"Generated profile failed schema validation:\n{formatted}")

    write_profile_yaml(args.output, profile, template_path=args.template, force=args.force)
    print(f"Wrote generated profile: {args.output}")
    print("Review geography, work authorization, compensation, and hard filters before using it.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except GenerateProfileError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1)
