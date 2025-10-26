"""Generate Steam discussion search URLs for app IDs.

This script reads Steam application IDs from a text file, determines how many
pages of discussion search results exist for each ID, and writes every
corresponding page URL to a CSV file.

Example
-------
    python generate_discussion_urls.py --ids ids.txt --output steam_urls.csv
"""
from __future__ import annotations

import argparse
import csv
import logging
import re
import sys
import time
from pathlib import Path
from typing import Iterable, List, Sequence

import requests
from bs4 import BeautifulSoup

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

BASE_URL_TEMPLATE = (
    "https://steamcommunity.com/app/{app_id}/discussions/search/?"
    "gidforum=1638661595058265180&include_deleted=1&q=+"
)
PAGE_QUERY_TEMPLATE = "&p={page}"


class UrlGenerationError(Exception):
    """Raised when discussion URLs cannot be generated for an app ID."""


def read_app_ids(ids_path: Path) -> List[str]:
    """Read Steam application IDs from ``ids_path``.

    Lines that are empty or do not look like integer IDs are ignored.
    Duplicate IDs are kept so the output order mirrors the input file.
    """

    app_ids: List[str] = []
    for raw_line in ids_path.read_text(encoding="utf-8").splitlines():
        app_id = raw_line.strip()
        if not app_id or app_id.startswith("#"):
            continue
        if not app_id.isdigit():
            logging.debug("Skipping non-numeric line: %s", raw_line)
            continue
        app_ids.append(app_id)
    return app_ids


def build_base_url(app_id: str) -> str:
    """Return the search URL for the first page of discussions."""

    return BASE_URL_TEMPLATE.format(app_id=app_id)


def build_page_url(app_id: str, page: int) -> str:
    """Return the search URL for ``page`` of a discussion search."""

    return f"{build_base_url(app_id)}{PAGE_QUERY_TEMPLATE.format(page=page)}"


def extract_max_page(soup: BeautifulSoup) -> int:
    """Inspect a parsed HTML page and return the maximum pagination number."""

    max_page = 1

    # Prefer explicit pagination controls if available.
    pagination_containers = soup.select(
        ".forum_pagination, .search_pagination, .forum_paging, .searchPaging"
    )
    containers: Sequence[BeautifulSoup] = (
        pagination_containers if pagination_containers else (soup,)
    )

    for container in containers:
        for element in container.select("[data-page]"):
            page_value = element.get("data-page")
            if page_value and page_value.isdigit():
                max_page = max(max_page, int(page_value))

        for link in container.select('a[href*="&p="]'):
            href = link.get("href", "")
            match = re.search(r"[?&]p=(\d+)", href)
            if match:
                max_page = max(max_page, int(match.group(1)))

        # Some pages display text such as "Page 1 of 12".
        text = container.get_text(" ", strip=True)
        match = re.search(r"Page\s+\d+\s+of\s+(\d+)", text, flags=re.IGNORECASE)
        if match:
            max_page = max(max_page, int(match.group(1)))

    return max_page


def fetch_max_page(session: requests.Session, app_id: str, timeout: float) -> int:
    """Download the first page for ``app_id`` and determine the max page."""

    url = build_base_url(app_id)
    logging.debug("Fetching %s", url)

    try:
        response = session.get(url, timeout=timeout)
        response.raise_for_status()
    except requests.RequestException as exc:  # pragma: no cover - network failure
        raise UrlGenerationError(f"Failed to download {url}: {exc}") from exc

    soup = BeautifulSoup(response.text, "html.parser")
    max_page = extract_max_page(soup)
    logging.debug("Detected %s pages for %s", max_page, app_id)
    return max(max_page, 1)


def generate_urls(
    app_ids: Iterable[str],
    session: requests.Session,
    timeout: float,
    delay: float,
) -> Iterable[tuple[str, str]]:
    """Yield ``(app_id, url)`` pairs for each discussion search page."""

    for app_id in app_ids:
        try:
            max_page = fetch_max_page(session, app_id, timeout=timeout)
        except UrlGenerationError as exc:
            logging.error("%s", exc)
            max_page = 1

        base_url = build_base_url(app_id)
        yield app_id, base_url

        for page in range(2, max_page + 1):
            yield app_id, build_page_url(app_id, page)

        if delay:
            time.sleep(delay)


def write_csv(rows: Iterable[tuple[str, str]], output_path: Path) -> None:
    """Write generated ``rows`` to ``output_path`` in CSV format."""

    with output_path.open("w", newline="", encoding="utf-8") as csv_file:
        writer = csv.writer(csv_file)
        writer.writerow(["app_id", "url"])
        for app_id, url in rows:
            writer.writerow([app_id, url])


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--ids",
        type=Path,
        default=Path("ids.txt"),
        help="Path to the text file containing Steam app IDs (default: ids.txt)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("steam_discussion_urls.csv"),
        help=(
            "Path to the CSV file that will contain the generated URLs "
            "(default: steam_discussion_urls.csv)"
        ),
    )
    parser.add_argument(
        "--user-agent",
        default=DEFAULT_USER_AGENT,
        help="User-Agent header to send with HTTP requests.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=30.0,
        help="Timeout in seconds for HTTP requests (default: 30).",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=0.0,
        help="Optional delay in seconds between requests to avoid rate limits.",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        help="Logging level for script output (default: INFO).",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])

    logging.basicConfig(level=args.log_level, format="%(levelname)s: %(message)s")

    if not args.ids.exists():
        logging.error("IDs file not found: %s", args.ids)
        return 1

    app_ids = read_app_ids(args.ids)
    if not app_ids:
        logging.error("No valid app IDs found in %s", args.ids)
        return 1

    session = requests.Session()
    session.headers.update({"User-Agent": args.user_agent})

    url_rows = generate_urls(app_ids, session=session, timeout=args.timeout, delay=args.delay)
    write_csv(url_rows, args.output)

    logging.info("Wrote URLs for %d app IDs to %s", len(app_ids), args.output)
    return 0


if __name__ == "__main__":  # pragma: no cover - script entry point
    raise SystemExit(main())
