#!/usr/bin/env python3
"""Fetch top-level Hacker News comments for a Who is Hiring thread."""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

try:
    import requests
except ImportError:  # pragma: no cover - exercised only in incomplete environments.
    requests = None  # type: ignore[assignment]

try:
    from bs4 import BeautifulSoup
except ImportError:  # pragma: no cover - exercised only in incomplete environments.
    BeautifulSoup = None  # type: ignore[assignment,misc]


HN_API_ITEM_URL = "https://hacker-news.firebaseio.com/v0/item/{item_id}.json"
HN_ITEM_URL = "https://news.ycombinator.com/item?id={item_id}"
DEFAULT_OUTPUT_DIR = Path("data/raw")
DEFAULT_DELAY_SECONDS = 0.05
REQUEST_TIMEOUT_SECONDS = 20


class FetchError(RuntimeError):
    """Raised when a Hacker News item cannot be fetched or validated."""


def ensure_dependencies() -> None:
    missing: list[str] = []
    if requests is None:
        missing.append("requests")
    if BeautifulSoup is None:
        missing.append("beautifulsoup4")

    if missing:
        packages = ", ".join(missing)
        raise FetchError(
            f"Missing required package(s): {packages}. "
            "Install dependencies with: python3 -m pip install -r requirements.txt"
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fetch top-level comments from a Hacker News Who is Hiring thread."
    )
    source_group = parser.add_mutually_exclusive_group(required=True)
    source_group.add_argument(
        "--item-id",
        type=int,
        help="Hacker News thread item id, for example 48747975.",
    )
    source_group.add_argument(
        "--url",
        help='Hacker News thread URL, for example "https://news.ycombinator.com/item?id=48747975".',
    )
    parser.add_argument(
        "--month",
        required=True,
        help="Report month in YYYY-MM format, used in the output filename.",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=DEFAULT_DELAY_SECONDS,
        help=f"Delay in seconds between comment fetches. Default: {DEFAULT_DELAY_SECONDS}.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help=f"Directory for raw JSON output. Default: {DEFAULT_OUTPUT_DIR}.",
    )
    return parser.parse_args()


def validate_month(month: str) -> None:
    if not re.fullmatch(r"\d{4}-\d{2}", month):
        raise FetchError(f"Invalid --month '{month}'. Expected format YYYY-MM.")

    try:
        datetime.strptime(month, "%Y-%m")
    except ValueError as exc:
        raise FetchError(f"Invalid --month '{month}'. Expected a real calendar month.") from exc


def extract_item_id_from_url(url: str) -> int:
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    item_ids = query.get("id")

    if not item_ids or not item_ids[0].strip():
        raise FetchError(f"Could not find an item id in URL: {url}")

    try:
        return int(item_ids[0])
    except ValueError as exc:
        raise FetchError(f"URL item id is not an integer: {item_ids[0]}") from exc


def resolve_item_id(args: argparse.Namespace) -> int:
    if args.item_id is not None:
        return args.item_id
    return extract_item_id_from_url(args.url)


def fetch_item(item_id: int, session: Any) -> dict[str, Any] | None:
    url = HN_API_ITEM_URL.format(item_id=item_id)
    try:
        response = session.get(url, timeout=REQUEST_TIMEOUT_SECONDS)
        response.raise_for_status()
    except requests.RequestException as exc:  # type: ignore[union-attr]
        raise FetchError(f"Failed to fetch HN item {item_id}: {exc}") from exc

    try:
        item = response.json()
    except ValueError as exc:
        raise FetchError(f"HN API returned invalid JSON for item {item_id}.") from exc

    if item is not None and not isinstance(item, dict):
        raise FetchError(f"HN API returned an unexpected payload for item {item_id}.")

    return item


def validate_thread_item(item_id: int, item: dict[str, Any] | None) -> dict[str, Any]:
    if item is None:
        raise FetchError(f"HN item {item_id} does not exist.")

    kids = item.get("kids")
    if not isinstance(kids, list) or not kids:
        raise FetchError(f"HN item {item_id} has no top-level comments in 'kids'.")

    return item


def html_to_plain_text(html_text: str) -> str:
    if BeautifulSoup is None:
        raise FetchError("Missing required package: beautifulsoup4.")

    soup = BeautifulSoup(html_text, "html.parser")
    return soup.get_text(separator="\n").strip()


def unix_time_to_iso(timestamp: Any) -> str | None:
    if not isinstance(timestamp, int):
        return None
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat()


def normalize_comment(
    item: dict[str, Any],
    *,
    source_thread_id: int,
    source_thread_title: str,
    position_index: int,
) -> dict[str, Any] | None:
    if item.get("deleted") or item.get("dead"):
        return None

    comment_id = item.get("id")
    if not isinstance(comment_id, int):
        return None

    html_text = item.get("text") or ""
    if not isinstance(html_text, str):
        html_text = str(html_text)

    return {
        "id": comment_id,
        "parent": item.get("parent"),
        "by": item.get("by"),
        "time": item.get("time"),
        "time_iso": unix_time_to_iso(item.get("time")),
        "html_text": html_text,
        "plain_text": html_to_plain_text(html_text),
        "hn_url": HN_ITEM_URL.format(item_id=comment_id),
        "source_thread_id": source_thread_id,
        "source_thread_title": source_thread_title,
        "position_index": position_index,
    }


def fetch_top_level_comments(
    *,
    thread_id: int,
    thread_title: str,
    comment_ids: list[Any],
    session: Any,
    delay_seconds: float,
) -> list[dict[str, Any]]:
    comments: list[dict[str, Any]] = []
    total = len(comment_ids)

    for position_index, raw_comment_id in enumerate(comment_ids):
        if not isinstance(raw_comment_id, int):
            print(
                f"[{position_index + 1}/{total}] Skipping non-integer comment id: {raw_comment_id}",
                file=sys.stderr,
            )
            continue

        print(f"[{position_index + 1}/{total}] Fetching comment {raw_comment_id}...")
        item = fetch_item(raw_comment_id, session)
        if item is None:
            print(f"  Skipping missing comment {raw_comment_id}.", file=sys.stderr)
        else:
            comment = normalize_comment(
                item,
                source_thread_id=thread_id,
                source_thread_title=thread_title,
                position_index=position_index,
            )
            if comment is None:
                print(f"  Skipping deleted/dead/invalid comment {raw_comment_id}.")
            else:
                comments.append(comment)

        if delay_seconds > 0 and position_index < total - 1:
            time.sleep(delay_seconds)

    return comments


def build_output_payload(
    *,
    thread_id: int,
    thread_item: dict[str, Any],
    comments: list[dict[str, Any]],
) -> dict[str, Any]:
    title = thread_item.get("title") or ""
    if not isinstance(title, str):
        title = str(title)

    return {
        "thread": {
            "id": thread_id,
            "title": title,
            "url": HN_ITEM_URL.format(item_id=thread_id),
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "top_level_comment_count": len(comments),
        },
        "comments": comments,
    }


def write_output(payload: dict[str, Any], *, month: str, output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"hn_whos_hiring_{month}_raw.json"

    with output_path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False)
        handle.write("\n")

    return output_path


def run(args: argparse.Namespace) -> Path:
    validate_month(args.month)
    if args.delay < 0:
        raise FetchError("--delay must be greater than or equal to 0.")

    thread_id = resolve_item_id(args)
    ensure_dependencies()
    session = requests.Session()  # type: ignore[union-attr]

    print(f"Fetching thread {thread_id}...")
    thread_item = validate_thread_item(thread_id, fetch_item(thread_id, session))
    thread_title = str(thread_item.get("title") or "")
    comment_ids = thread_item["kids"]

    print(f"Thread title: {thread_title or '(untitled)'}")
    print(f"Found {len(comment_ids)} top-level comment ids.")

    comments = fetch_top_level_comments(
        thread_id=thread_id,
        thread_title=thread_title,
        comment_ids=comment_ids,
        session=session,
        delay_seconds=args.delay,
    )

    payload = build_output_payload(
        thread_id=thread_id,
        thread_item=thread_item,
        comments=comments,
    )
    output_path = write_output(payload, month=args.month, output_dir=args.output_dir)

    print(f"Saved {len(comments)} comments to {output_path}")
    return output_path


def main() -> int:
    args = parse_args()
    try:
        run(args)
    except FetchError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
