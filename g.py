"""Command-line entry point for the Ground News scraper.

Usage:
    uv run python g.py "https://ground.news/article/..."
    uv run python g.py URL1 URL2 --headed --min-interval 6
    uv run python g.py --feed "https://ground.news/interest/stock-markets" --limit 5

The GUI calls this script with a single URL, and ``gn.py`` calls it once per
URL. Pacing is shared across those processes through ``state/rate_limit.json``.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from playwright.sync_api import Error as PlaywrightError

from rate_limit import RateLimitError
from scraper import (
    DEFAULT_OUTPUT_DIR,
    DEFAULT_RATE_LIMIT_STATE,
    DEFAULT_STORAGE_STATE,
    GroundNewsScraper,
    ScrapeConfig,
    ScrapeError,
)

logger = logging.getLogger("groundnews")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Scrape Ground News article pages (coverage counts, perspective summaries, sources).",
    )
    parser.add_argument("urls", nargs="*", metavar="URL", help="Ground News article URL(s)")
    parser.add_argument(
        "--feed",
        action="extend",
        nargs="+",
        default=[],
        metavar="URL",
        help="topic/feed page (e.g. https://ground.news/interest/stock-markets); its article links are scraped",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        metavar="N",
        help="max articles to take from each --feed page (0 = all found; default: 0)",
    )
    parser.add_argument(
        "-o",
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help=f"where to write story JSON files (default: {DEFAULT_OUTPUT_DIR})",
    )
    parser.add_argument(
        "--storage-state",
        type=Path,
        default=DEFAULT_STORAGE_STATE,
        help=f"Playwright session file created by login.py (default: {DEFAULT_STORAGE_STATE})",
    )
    parser.add_argument(
        "--no-auth",
        action="store_true",
        help="ignore any saved session and scrape anonymously",
    )
    parser.add_argument(
        "--headed",
        action="store_true",
        help="show the browser window instead of running headless",
    )
    parser.add_argument(
        "--min-interval",
        type=float,
        default=4.0,
        metavar="SECONDS",
        help="minimum seconds between requests to the same host (default: 4.0)",
    )
    parser.add_argument(
        "--jitter",
        type=float,
        default=2.0,
        metavar="SECONDS",
        help="random extra delay added to --min-interval (default: 2.0)",
    )
    parser.add_argument(
        "--source-interval",
        type=float,
        default=1.5,
        metavar="SECONDS",
        help="minimum seconds between any two cited-article fetches (default: 1.5)",
    )
    parser.add_argument(
        "--max-load-more",
        type=int,
        default=40,
        metavar="BATCHES",
        help="cap on 'load more stories' clicks per article (default: 40)",
    )
    parser.add_argument(
        "--no-source-text",
        action="store_true",
        help="store sources without downloading each cited article's text",
    )
    parser.add_argument(
        "--respect-robots",
        action="store_true",
        help="skip cited articles whose robots.txt disallows fetching",
    )
    parser.add_argument(
        "--block-resources",
        action="store_true",
        help="block images, fonts, and media to reduce page weight",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=60.0,
        metavar="SECONDS",
        help="per-page navigation timeout (default: 60)",
    )
    parser.add_argument(
        "--retries",
        type=int,
        default=3,
        metavar="N",
        help="retries after a failed navigation or HTTP/network error (default: 3)",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="log debug details, including pacing waits",
    )
    return parser


def _validate(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    if args.min_interval < 0 or args.jitter < 0 or args.source_interval < 0:
        parser.error("intervals and jitter must be non-negative")
    if args.max_load_more < 0:
        parser.error("--max-load-more must be non-negative")
    if args.limit < 0:
        parser.error("--limit must be non-negative")
    if args.timeout <= 0:
        parser.error("--timeout must be greater than zero")
    if args.retries < 1:
        parser.error("--retries must be at least 1")


def _configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(message)s",
        stream=sys.stdout,
        force=True,
    )
    # Playwright is chatty at DEBUG level and drowns out pacing messages.
    logging.getLogger("asyncio").setLevel(logging.WARNING)


def build_config(args: argparse.Namespace) -> ScrapeConfig:
    return ScrapeConfig(
        output_dir=Path(args.output_dir),
        storage_state=None if args.no_auth else Path(args.storage_state),
        rate_limit_state=DEFAULT_RATE_LIMIT_STATE,
        headless=not args.headed,
        block_resources=args.block_resources,
        min_interval=args.min_interval,
        jitter=args.jitter,
        source_interval=args.source_interval,
        max_load_more_batches=args.max_load_more,
        page_timeout_ms=int(args.timeout * 1_000),
        fetch_source_text=not args.no_source_text,
        respect_robots=args.respect_robots,
        max_retries=args.retries,
    )


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _validate(parser, args)
    _configure_logging(args.verbose)

    urls = [url.strip() for url in args.urls if url.strip()]
    feeds = [feed.strip() for feed in args.feed if feed.strip()]
    if not urls and not feeds:
        parser.error("provide at least one article URL or --feed page")

    config = build_config(args)
    failures = 0

    try:
        with GroundNewsScraper(config) as scraper:
            targets = list(urls)
            for feed in feeds:
                try:
                    found = scraper.scrape_feed(feed, limit=args.limit)
                except ScrapeError as exc:
                    failures += 1
                    logger.error("Failed to read feed %s: %s", feed, exc)
                except PlaywrightError as exc:
                    failures += 1
                    logger.error("Browser error while reading feed %s: %s", feed, exc)
                else:
                    targets.extend(found)

            targets = list(dict.fromkeys(targets))
            if not targets:
                logger.error("No article URLs to scrape")
                return 1
            logger.info("Scraping %d article(s)", len(targets))

            for url in targets:
                try:
                    path = scraper.scrape(url)
                except ScrapeError as exc:
                    failures += 1
                    logger.error("Failed to scrape %s: %s", url, exc)
                except PlaywrightError as exc:
                    failures += 1
                    logger.error("Browser error while scraping %s: %s", url, exc)
                except OSError as exc:
                    failures += 1
                    logger.error("Could not save the result for %s: %s", url, exc)
                else:
                    print(f"Data successfully saved to {path}")
    except RateLimitError as exc:
        logger.error("Stopping: %s", exc)
        return 1
    except PlaywrightError as exc:
        logger.error("Playwright could not start: %s", exc)
        logger.error("Install the browser once with: uv run playwright install chromium")
        return 1
    except KeyboardInterrupt:
        logger.warning("Interrupted")
        return 130

    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
