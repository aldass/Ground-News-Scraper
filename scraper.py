"""Playwright-based scraper for Ground News article pages.

The public entry points are :class:`GroundNewsScraper` and the pure helpers in
this module. ``g.py`` is the command-line wrapper used by the GUI and by
``gn.py``.

Selectors were verified against the live page. Ground News is a Next.js app,
so the code waits for elements instead of sleeping fixed amounts, dismisses the
onboarding dialog that intercepts clicks, and reuses one browser context for
the whole run.

Every request goes through :class:`rate_limit.FileRateLimiter` so that a batch
of URLs cannot hammer Ground News or any cited news site.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from datetime import datetime
from functools import partial
from pathlib import Path
from typing import Callable
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser

import trafilatura
from playwright.sync_api import (
    Browser,
    BrowserContext,
    Error as PlaywrightError,
    Locator,
    Page,
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)
from tqdm import tqdm

from rate_limit import (
    GLOBAL_KEY,
    RETRYABLE_STATUS,
    FileRateLimiter,
    backoff_delay,
    host_key,
    parse_retry_after,
)

logger = logging.getLogger("groundnews")

PROJECT_DIR = Path(__file__).resolve().parent
DEFAULT_OUTPUT_DIR = PROJECT_DIR / "json"
DEFAULT_STORAGE_STATE = PROJECT_DIR / "auth" / "storage_state.json"
DEFAULT_RATE_LIMIT_STATE = PROJECT_DIR / "state" / "rate_limit.json"

USER_AGENT_TEMPLATE = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/{major}.0.0.0 Safari/537.36"
)

FETCH_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://ground.news/",
}

# Article page structure. The absolute XPaths are brittle but match the live
# site; each has a semantic fallback where one is trustworthy.
SELECTOR_TITLE = ("#titleArticle", "h1")
SELECTOR_TOTAL_SOURCES = (
    "xpath=/html/body/main/div/article/div/div/div[4]/div[1]/div/div/span[2]",
)
SELECTOR_LEANING_LEFT = (
    "xpath=/html/body/main/div/article/div/div/div[4]/div[1]/div/span[2]",
)
SELECTOR_LEANING_RIGHT = (
    "xpath=/html/body/main/div/article/div/div/div[4]/div[1]/div/span[4]",
)
SELECTOR_CENTER = (
    "xpath=/html/body/main/div/article/div/div/div[4]/div[1]/div/span[6]",
)
SELECTOR_SUMMARY_POINTS = (
    "xpath=/html/body/main/div/article/div/div/div[1]/div[2]/div[3]/div/div/div[2]/div[1]/ul/li",
)
SELECTOR_SOURCE_CARD = "#article-summary"
SELECTOR_MORE_STORIES = "#more-stories"
SELECTOR_SOURCE_TITLE = ("xpath=.//a/h4", "xpath=.//h4")
SELECTOR_SOURCE_LINK = ("xpath=./div/a", "xpath=.//a[h4]")
SELECTOR_SOURCE_BIAS = (
    "xpath=.//a[contains(@id,'article-source-bias')]/div",
    '[id^="article-source-bias"] div',
)
SELECTOR_FEED_ARTICLE = 'a[href*="/article/"]'

ONBOARDING_DIALOG = '[data-testid="onboarding-dialog"]'
ONBOARDING_CLOSE = '[data-testid="onboarding-close-button"]'
DIALOG_CLOSE = '[data-slot="dialog-close"]'

BLOCKED_RESOURCE_TYPES = frozenset({"image", "font", "media"})

# Playwright errors that are worth retrying; a closed browser is not.
TRANSIENT_NETWORK_MARKERS = (
    "net::",
    "ERR_CONNECTION_",
    "ERR_NETWORK_CHANGED",
    "ERR_TIMED_OUT",
    "Timeout",
)

SHARE_HOSTS = frozenset(
    {
        "twitter.com",
        "x.com",
        "facebook.com",
        "linkedin.com",
        "reddit.com",
        "t.me",
        "wa.me",
        "threads.net",
        "bsky.app",
    }
)

ROBOTS_TIMEOUT = 10.0


class ScrapeError(RuntimeError):
    """Raised when an article could not be scraped."""


@dataclass
class ScrapeConfig:
    """Everything tunable about one scraping run."""

    output_dir: Path = DEFAULT_OUTPUT_DIR
    storage_state: Path | None = DEFAULT_STORAGE_STATE
    rate_limit_state: Path = DEFAULT_RATE_LIMIT_STATE

    headless: bool = True
    user_agent: str | None = None
    block_resources: bool = False

    # Pacing: every request to a host waits min_interval + up to jitter seconds.
    min_interval: float = 4.0
    jitter: float = 2.0
    # Extra floor shared by all source sites, so a long source list cannot burst.
    source_interval: float = 1.5
    source_jitter: float = 1.0

    max_load_more_batches: int = 40
    page_timeout_ms: int = 60_000
    load_more_timeout_ms: int = 8_000
    load_more_settle_ms: int = 10_000
    summary_settle_ms: int = 3_000
    request_timeout_ms: int = 30_000
    max_retries: int = 3
    # Upper bound on any single backoff sleep, including a server Retry-After.
    max_retry_wait: float = 300.0

    fetch_source_text: bool = True
    respect_robots: bool = False
    show_progress: bool | None = None

    def progress_enabled(self) -> bool:
        if self.show_progress is None:
            return sys.stderr.isatty()
        return self.show_progress


def browser_user_agent(browser: Browser) -> str:
    """Build a user agent that matches the Chromium build Playwright launched."""
    major = (browser.version or "").split(".")[0]
    if not major.isdigit():
        major = "141"
    return USER_AGENT_TEMPLATE.format(major=major)


def _usable_storage_state(path: Path | None) -> Path | None:
    """Return the session file if it exists and parses, else ``None``.

    A truncated file (interrupted login, manual edit) would otherwise make
    Playwright fail at context creation with a misleading browser error.
    """
    if path is None:
        return None
    candidate = Path(path).expanduser()
    if not candidate.is_file():
        return None
    try:
        data = json.loads(candidate.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Ignoring the session file %s (%s); run login.py again", candidate, exc)
        return None
    if not isinstance(data, dict) or not isinstance(data.get("cookies"), list):
        logger.warning("Ignoring the session file %s: it is not a Playwright storage state", candidate)
        return None
    return candidate


def _pace(limiter: FileRateLimiter | None, key: str, interval: float, jitter: float) -> None:
    if limiter is not None and interval > 0:
        limiter.wait(key, interval, jitter)


def _pace_page(limiter: FileRateLimiter | None, url: str, config: ScrapeConfig) -> None:
    _pace(limiter, host_key(url), config.min_interval, config.jitter)


def _pace_source(limiter: FileRateLimiter | None, url: str, config: ScrapeConfig) -> None:
    if limiter is None:
        return
    # Reserved together so the global floor is measured from the request time,
    # not from before a per-host wait.
    limiter.wait_many(
        [
            (GLOBAL_KEY, config.source_interval, config.source_jitter),
            (host_key(url), config.min_interval, config.jitter),
        ]
    )


def _first_text(scope: Page | Locator, selectors: tuple[str, ...], *, timeout_ms: int = 2_000) -> str | None:
    for selector in selectors:
        try:
            locator = scope.locator(selector)
            if locator.count() == 0:
                continue
            text = locator.first.inner_text(timeout=timeout_ms).strip()
            if text:
                return text
        except PlaywrightError:
            continue
    return None


def _is_cited_article(url: str) -> bool:
    """True for an outlet's article URL, false for Ground News' own pages."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    if parts.scheme not in ("http", "https"):
        return False
    host = parts.netloc.lower()
    return bool(host) and host != "ground.news" and not host.endswith(".ground.news")


def _is_share_url(url: str) -> bool:
    host = urlsplit(url).netloc.lower()
    return any(host == share or host.endswith("." + share) for share in SHARE_HOSTS)


def _is_transient_network_error(exc: PlaywrightError) -> bool:
    message = str(exc)
    return any(marker in message for marker in TRANSIENT_NETWORK_MARKERS)


def _source_link(card: Locator) -> str | None:
    """Return the cited outlet's URL for one source card.

    Most cards wrap the headline in a link to the article. Some render the
    headline outside the anchor; for those the article is the only absolute,
    non-Ground-News URL in the card. Share links are skipped so they cannot
    win over the article they share.
    """
    candidates: list[str] = []
    for selector in SELECTOR_SOURCE_LINK:
        try:
            locator = card.locator(selector)
            if locator.count() == 0:
                continue
            href = locator.first.get_attribute("href")
        except PlaywrightError:
            continue
        if href:
            candidates.append(href)

    try:
        anchors = card.locator("a[href]")
        for index in range(anchors.count()):
            href = anchors.nth(index).get_attribute("href")
            if href:
                candidates.append(href)
    except PlaywrightError:
        pass

    for href in candidates:
        try:
            resolved = urljoin("https://ground.news/", href.strip())
        except ValueError:
            continue
        if _is_cited_article(resolved) and not _is_share_url(resolved):
            return resolved
    return None


def dismiss_blocking_dialogs(page: Page) -> bool:
    """Close a dialog that intercepts clicks, if one is open.

    Ground News shows an onboarding/subscribe dialog to anonymous visitors.
    A consent or share dialog uses the same close slot, so the generic close
    is tried as well. Returns ``True`` when something was closed.
    """
    dismissed = False
    try:
        page.wait_for_selector(ONBOARDING_DIALOG, state="visible", timeout=1_500)
    except PlaywrightTimeoutError:
        pass
    else:
        dismissed = _click_visible_close(page, (ONBOARDING_CLOSE, DIALOG_CLOSE), ONBOARDING_DIALOG)
        if not dismissed:
            page.keyboard.press("Escape")
            try:
                page.wait_for_selector(ONBOARDING_DIALOG, state="detached", timeout=3_000)
                dismissed = True
            except PlaywrightTimeoutError:
                logger.warning("An overlay is still blocking the page; clicks may fail")

    if not dismissed and _click_visible_close(page, (DIALOG_CLOSE,), None):
        dismissed = True
    return dismissed


def _click_visible_close(page: Page, selectors: tuple[str, ...], dialog: str | None) -> bool:
    """Click the first visible close button, verifying ``dialog`` is gone."""
    for selector in selectors:
        button = page.locator(selector)
        try:
            if button.count() == 0 or not button.first.is_visible():
                continue
            button.first.click(timeout=3_000)
        except PlaywrightError:
            continue
        if dialog is None:
            try:
                return not button.first.is_visible()
            except PlaywrightError:
                return True
        try:
            page.wait_for_selector(dialog, state="detached", timeout=5_000)
            return True
        except PlaywrightTimeoutError:
            continue
    return False


def _click_with_dialog_retry(page: Page, locator: Locator, timeout_ms: int) -> None:
    """Click, retrying once after dismissing a dialog that appeared late."""
    try:
        locator.click(timeout=timeout_ms)
    except PlaywrightTimeoutError:
        if not dismiss_blocking_dialogs(page):
            raise
        locator.click(timeout=timeout_ms)


def _read_points(page: Page) -> list[str]:
    for selector in SELECTOR_SUMMARY_POINTS:
        try:
            locator = page.locator(selector)
            if locator.count() == 0:
                continue
            return [text.strip() for text in locator.all_inner_texts() if text.strip()]
        except PlaywrightError:
            continue
    return []


def _wait_for_points(page: Page, previous: list[str], timeout_ms: int) -> list[str]:
    """Return the new, fully rendered bullet list, or ``[]`` if it never changed.

    Two consecutive equal reads are required, so a half-drawn list is not
    captured. A list equal to the previous side's means the click had no
    effect; attributing it to this side would silently duplicate another
    perspective, so it is dropped instead.
    """
    deadline = time.monotonic() + timeout_ms / 1_000
    stable: list[str] = []
    while True:
        current = _read_points(page)
        if current and current != previous and current == stable:
            return current
        stable = current
        if time.monotonic() >= deadline:
            return current if current and current != previous else []
        page.wait_for_timeout(300)


def collect_perspective(
    page: Page,
    side: str,
    previous: list[str],
    *,
    settle_ms: int,
    click_timeout_ms: int = 8_000,
) -> list[str]:
    """Open the ``left``/``center``/``right`` summary and return its bullet points."""
    button = page.locator(f"#{side}-summary-button")
    if button.count() == 0:
        logger.info("%s summary button not found; keeping it empty", side.capitalize())
        return []
    if not button.first.is_visible() or not button.first.is_enabled():
        logger.info("%s summary button is not clickable; keeping it empty", side.capitalize())
        return []

    try:
        _click_with_dialog_retry(page, button.first, timeout_ms=click_timeout_ms)
    except PlaywrightError as exc:
        logger.warning("Could not open the %s summary (%s); keeping it empty", side, exc.__class__.__name__)
        return []

    points = _wait_for_points(page, previous, settle_ms)
    if points:
        logger.info("%s perspective: %d points", side.capitalize(), len(points))
    else:
        logger.warning(
            "%s perspective did not render after the click; keeping it empty", side.capitalize()
        )
    return points


def _count_when_stable(page: Page, cards: Locator, *, timeout_ms: int) -> int:
    """Wait until the card count stops changing, then return it."""
    deadline = time.monotonic() + timeout_ms / 1_000
    last = cards.count()
    while time.monotonic() < deadline:
        page.wait_for_timeout(250)
        current = cards.count()
        if current == last:
            return current
        last = current
    return cards.count()


def collect_article_links(page: Page, *, limit: int = 0) -> list[str]:
    """Return de-duplicated Ground News article URLs from a feed/topic page.

    Only links to ``/article/`` are kept, query strings and fragments are
    dropped, and ``limit`` caps the result (``0`` means no cap).
    """
    anchors = page.locator(SELECTOR_FEED_ARTICLE)
    links: list[str] = []
    seen: set[str] = set()

    for index in range(anchors.count()):
        try:
            href = anchors.nth(index).get_attribute("href")
        except PlaywrightError:
            continue
        if not href:
            continue
        try:
            parts = urlsplit(urljoin("https://ground.news/", href.strip()))
        except ValueError:
            continue
        if parts.netloc.lower() not in ("ground.news", "www.ground.news"):
            continue
        if not parts.path.startswith("/article/") or len(parts.path) <= len("/article/"):
            continue

        article = f"https://ground.news{parts.path.rstrip('/')}"
        if article in seen:
            continue
        seen.add(article)
        links.append(article)
        if limit and len(links) >= limit:
            break

    return links


def load_all_stories(
    page: Page,
    *,
    config: ScrapeConfig,
    limiter: FileRateLimiter | None = None,
    expected_total: int = 0,
) -> int:
    """Click "load more stories" until every source is listed; return the count."""
    cards = page.locator(SELECTOR_SOURCE_CARD)
    count = _count_when_stable(page, cards, timeout_ms=min(config.load_more_settle_ms, 3_000))
    batches = 0

    while batches < config.max_load_more_batches:
        if expected_total and count >= expected_total:
            logger.info("All %d sources are already listed", count)
            return count

        button = page.locator(SELECTOR_MORE_STORIES)
        if button.count() == 0 or not button.first.is_visible():
            return count

        _pace_page(limiter, page.url, config)
        try:
            _click_with_dialog_retry(page, button.first, config.load_more_timeout_ms)
        except PlaywrightTimeoutError:
            logger.info("The 'load more stories' button stopped responding")
            return count

        grew = _wait_until(
            lambda current=count: cards.count() > current,
            timeout_ms=config.load_more_settle_ms,
            page=page,
        )
        new_count = cards.count()
        if not grew:
            logger.info("No further sources appeared; stopping at %d", count)
            return count

        batches += 1
        count = new_count
        logger.info("Loaded %d sources so far (batch %d)", count, batches)

    if config.max_load_more_batches:
        logger.warning(
            "Stopped after %d batches; some sources may be missing (raise --max-load-more to load more)",
            config.max_load_more_batches,
        )
    return count


def _wait_until(predicate: Callable[[], bool], *, timeout_ms: int, page: Page) -> bool:
    deadline = time.monotonic() + timeout_ms / 1_000
    while time.monotonic() < deadline:
        if predicate():
            return True
        page.wait_for_timeout(250)
    return predicate()


def extract_article_data(page: Page, *, summary_settle_ms: int = 3_000) -> dict:
    """Read the headline, coverage counts, and perspective summaries."""
    title = _first_text(page, SELECTOR_TITLE)
    if not title:
        raise ScrapeError(
            "The article title did not appear. The page may be blocked, behind a login, "
            "or the URL may not be a Ground News article."
        )

    data: dict = {"title": title}
    fields = {
        "total_source": SELECTOR_TOTAL_SOURCES,
        "leaning_left": SELECTOR_LEANING_LEFT,
        "leaning_right": SELECTOR_LEANING_RIGHT,
        "center": SELECTOR_CENTER,
    }
    for field, selectors in fields.items():
        value = _first_text(page, selectors)
        if value is None:
            logger.warning("Could not read %s from the coverage breakdown", field)
        data[field] = value or ""

    previous: list[str] = _read_points(page)
    for side in ("left", "center", "right"):
        # The left tab is often the default view, so a stable list is accepted
        # for it; the other two must differ from the previous side.
        baseline = previous if side != "left" else []
        points = collect_perspective(page, side, baseline, settle_ms=summary_settle_ms)
        data[f"{side}_points"] = points
        if points:
            previous = points

    return data


def extract_sources(
    page: Page,
    *,
    fetch_text: Callable[[str], str | None] | None = None,
    show_progress: bool = False,
) -> list[dict]:
    """Read every source card, optionally fetching each article's full text."""
    cards = page.locator(SELECTOR_SOURCE_CARD)
    total = cards.count()
    if total == 0:
        logger.warning("No source cards were found on the page")
        return []

    sources: list[dict] = []
    for index in tqdm(
        range(total),
        desc="Processing sources",
        unit="source",
        disable=not show_progress,
    ):
        card = cards.nth(index)
        try:
            title = _first_text(card, SELECTOR_SOURCE_TITLE, timeout_ms=1_000)
            link = _source_link(card)
            if not link:
                logger.warning("Source %d has no article link; skipping it", index)
                continue

            bias = "unknown"
            try:
                bias = _first_text(card, SELECTOR_SOURCE_BIAS, timeout_ms=1_000) or "unknown"
            except PlaywrightError as exc:
                logger.debug("Source %d has no bias rating: %s", index, exc)

            text = fetch_text(link) if fetch_text else None
            sources.append(
                {
                    "news_title": title or "",
                    "news_link": link,
                    "bias": bias,
                    "text": text,
                }
            )
        except PlaywrightError as exc:
            logger.warning("Skipping source %d: %s", index, exc)

    logger.info("Sources extracted. Total: %d", len(sources))
    return sources


class RobotsPolicy:
    """Cache and consult ``robots.txt`` once per host.

    Opt-in: some news sites disallow crawling, and the pre-Playwright scraper
    never checked. Failures to read robots.txt allow the fetch (the common
    convention) and are logged at debug level.
    """

    def __init__(self, user_agent: str, *, timeout: float = ROBOTS_TIMEOUT) -> None:
        self.user_agent = user_agent
        self.timeout = timeout
        self._cache: dict[str, RobotFileParser | None] = {}

    def allowed(self, url: str, *, limiter: FileRateLimiter | None = None) -> bool:
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            return False
        host = parts.netloc.lower()
        if host not in self._cache:
            self._cache[host] = self._load(parts.scheme, host, limiter)
        parser = self._cache[host]
        if parser is None:
            return True
        return parser.can_fetch(self.user_agent, url)

    def _load(self, scheme: str, host: str, limiter: FileRateLimiter | None) -> RobotFileParser | None:
        _pace(limiter, host, 1.0, 0.5)
        request = urllib.request.Request(
            f"{scheme}://{host}/robots.txt",
            headers={"User-Agent": self.user_agent},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = response.read().decode("utf-8", "replace")
        except (urllib.error.URLError, OSError) as exc:
            logger.debug("robots.txt unavailable for %s: %s", host, exc)
            return None

        parser = RobotFileParser()
        parser.parse(body.splitlines())
        return parser


def _retry_delay(headers: dict[str, str], attempt: int, config: ScrapeConfig) -> float:
    """Backoff for ``attempt``, preferring a (clamped) server ``Retry-After``."""
    retry_after = parse_retry_after(headers.get("retry-after"))
    delay = backoff_delay(attempt) if retry_after is None else retry_after
    return min(delay, config.max_retry_wait)


def fetch_article_text(
    context: BrowserContext,
    url: str,
    *,
    config: ScrapeConfig,
    limiter: FileRateLimiter | None = None,
    robots: RobotsPolicy | None = None,
) -> str | None:
    """Fetch a cited article through the browser context and extract its text.

    Retries throttling, server errors, and transient network failures with
    exponential backoff, honouring ``Retry-After`` when the site sends it (and
    clamping it to ``max_retry_wait``). Returns ``None`` when the page cannot be
    read (blocked, non-HTML, or unparseable) so one dead link does not abort the
    story.

    Redirects are followed by the request context; only the cited URL's host is
    paced and checked against robots.txt.
    """
    if not url.lower().startswith(("http://", "https://")):
        return None
    if robots is not None and not robots.allowed(url, limiter=limiter):
        logger.info("robots.txt disallows %s; skipping its full text", url)
        return None

    attempt = 0
    while True:
        _pace_source(limiter, url, config)
        try:
            response = context.request.get(
                url,
                headers=FETCH_HEADERS,
                timeout=config.request_timeout_ms,
                fail_on_status_code=False,
            )
        except PlaywrightError as exc:
            if attempt < config.max_retries and _is_transient_network_error(exc):
                attempt += 1
                delay = min(backoff_delay(attempt), config.max_retry_wait)
                logger.warning(
                    "Could not fetch %s (%s); retrying in %.0fs (retry %d/%d)",
                    url,
                    exc,
                    delay,
                    attempt,
                    config.max_retries,
                )
                time.sleep(delay)
                continue
            logger.warning("Could not fetch %s: %s", url, exc)
            return None

        try:
            status = response.status
            headers = {key.lower(): value for key, value in response.headers.items()}
            content_type = headers.get("content-type", "").lower()
            html = response.text() if status == 200 and "html" in content_type else None
        except PlaywrightError as exc:
            logger.warning("Could not read the response from %s: %s", url, exc)
            return None
        finally:
            response.dispose()

        if status == 200:
            if html is None:
                logger.info("Skipping %s: content type is %r", url, content_type)
                return None
            try:
                return trafilatura.extract(html, url=url)
            except Exception as exc:  # trafilatura raises a variety of errors
                logger.warning("Could not extract text from %s: %s", url, exc)
                return None

        if status in RETRYABLE_STATUS and attempt < config.max_retries:
            attempt += 1
            delay = _retry_delay(headers, attempt, config)
            logger.warning(
                "HTTP %d from %s; retrying in %.0fs (retry %d/%d)",
                status,
                url,
                delay,
                attempt,
                config.max_retries,
            )
            time.sleep(delay)
            continue

        logger.info("HTTP %d from %s; no text extracted", status, url)
        return None


def build_structured_data(
    url: str,
    article_data: dict,
    *,
    story_id: str | None = None,
    timestamp: datetime | None = None,
) -> dict:
    """Assemble the JSON document written to ``json/<story_id>.json``."""
    moment = timestamp or datetime.now()
    sid = story_id or f"GN_{moment.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"
    return {
        "story_id": sid,
        "metadata": {
            "title": article_data.get("title", ""),
            "timestamp": moment.isoformat(),
            "url": url,
        },
        "bias_distribution": {
            "total_sources": article_data.get("total_source", ""),
            "leaning_left": article_data.get("leaning_left", ""),
            "center": article_data.get("center", ""),
            "leaning_right": article_data.get("leaning_right", ""),
        },
        "perspective_summaries": {
            "left": article_data.get("left_points", []),
            "center": article_data.get("center_points", []),
            "right": article_data.get("right_points", []),
        },
        "sources": article_data.get("sources", []),
    }


def save_story(story: dict, output_dir: Path | str) -> Path:
    """Write the story atomically so an interrupted run leaves no truncated JSON."""
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    target = output_path / f"{story['story_id']}.json"
    tmp = target.with_name(f"{target.name}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(story, handle, indent=2, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, target)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    return target


def _parse_total(value: str) -> int:
    try:
        return int(value.replace(",", "").strip())
    except (AttributeError, ValueError):
        return 0


class GroundNewsScraper:
    """Owns one Playwright browser/context and scrapes URLs sequentially."""

    def __init__(self, config: ScrapeConfig | None = None) -> None:
        self.config = config or ScrapeConfig()
        self._playwright = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        self._limiter: FileRateLimiter | None = None
        self._robots: RobotsPolicy | None = None

    def __enter__(self) -> "GroundNewsScraper":
        self._playwright = sync_playwright().start()
        try:
            self._start_browser()
        except BaseException:
            self._shutdown()
            raise
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self._shutdown()

    def _start_browser(self) -> None:
        config = self.config
        self._browser = self._playwright.chromium.launch(  # type: ignore[union-attr]
            headless=config.headless,
            args=["--disable-blink-features=AutomationControlled"],
        )
        user_agent = config.user_agent or browser_user_agent(self._browser)

        context_kwargs: dict = {
            "user_agent": user_agent,
            "viewport": {"width": 1440, "height": 900},
            "locale": "en-US",
            "extra_http_headers": {"Accept-Language": "en-US,en;q=0.9"},
        }
        storage_state = _usable_storage_state(config.storage_state)
        if storage_state is not None:
            context_kwargs["storage_state"] = str(storage_state)
            logger.info("Using the saved Ground News session from %s", storage_state)
        else:
            logger.debug("No saved session; scraping anonymously")

        self._context = self._browser.new_context(**context_kwargs)
        self._context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
        )
        if config.block_resources:
            self._context.route("**/*", _route_handler)

        self._page = self._context.new_page()
        self._page.set_default_timeout(config.page_timeout_ms)
        self._page.set_default_navigation_timeout(config.page_timeout_ms)

        self._limiter = FileRateLimiter(
            config.rate_limit_state,
            max_wait=max(300.0, config.min_interval + config.jitter + config.source_jitter),
        )
        if config.respect_robots:
            self._robots = RobotsPolicy(user_agent)

    def _shutdown(self) -> None:
        for resource in (self._context, self._browser):
            if resource is not None:
                try:
                    resource.close()
                except PlaywrightError:
                    pass
        self._context = None
        self._browser = None
        self._page = None
        if self._playwright is not None:
            try:
                self._playwright.stop()
            except Exception:  # pragma: no cover - interpreter teardown
                pass
            self._playwright = None

    @property
    def page(self) -> Page:
        if self._page is None:
            raise RuntimeError("GroundNewsScraper must be used as a context manager")
        return self._page

    def scrape(self, url: str) -> Path:
        """Scrape one article and return the path of the JSON file it wrote."""
        self._navigate(url)
        article_data = extract_article_data(
            self.page, summary_settle_ms=self.config.summary_settle_ms
        )
        expected_total = _parse_total(article_data.get("total_source", ""))
        if expected_total:
            try:
                self.page.wait_for_selector(SELECTOR_SOURCE_CARD, state="attached", timeout=15_000)
            except PlaywrightTimeoutError as exc:
                raise ScrapeError(
                    f"Ground News reports {expected_total} sources but none rendered; "
                    "refusing to save an empty story."
                ) from exc

        load_all_stories(
            self.page,
            config=self.config,
            limiter=self._limiter,
            expected_total=expected_total,
        )

        fetch_text = None
        if self.config.fetch_source_text:
            fetch_text = partial(
                fetch_article_text,
                self._context,  # type: ignore[arg-type]
                config=self.config,
                limiter=self._limiter,
                robots=self._robots,
            )

        sources = extract_sources(
            self.page,
            fetch_text=fetch_text,
            show_progress=self.config.progress_enabled(),
        )
        if not sources:
            raise ScrapeError(
                "No sources could be read from the article; the source-list selectors may have changed."
            )
        article_data["sources"] = sources

        story = build_structured_data(url, article_data)
        path = save_story(story, self.config.output_dir)
        logger.info("Saved %s", path)
        return path

    def scrape_feed(self, feed_url: str, *, limit: int = 0) -> list[str]:
        """Navigate a topic/feed page and return the article URLs it lists."""
        self._load(feed_url)
        try:
            self.page.wait_for_selector(SELECTOR_FEED_ARTICLE, state="attached", timeout=15_000)
        except PlaywrightTimeoutError as exc:
            raise ScrapeError(
                f"No article links were found on {feed_url}; it may not be a Ground News feed page."
            ) from exc

        dismiss_blocking_dialogs(self.page)
        links = collect_article_links(self.page, limit=limit)
        if not links:
            raise ScrapeError(f"No Ground News article links were found on {feed_url}.")
        logger.info("Found %d article(s) on %s", len(links), feed_url)
        return links

    def _load(self, url: str) -> None:
        """Navigate to ``url`` with pacing, retries, and status handling."""
        config = self.config
        last_timeout: PlaywrightTimeoutError | None = None
        attempts = config.max_retries + 1

        for attempt in range(1, attempts + 1):
            _pace_page(self._limiter, url, config)
            try:
                response = self.page.goto(
                    url,
                    wait_until="domcontentloaded",
                    timeout=config.page_timeout_ms,
                )
            except PlaywrightTimeoutError as exc:
                last_timeout = exc
                logger.warning("Navigation timed out (attempt %d/%d)", attempt, attempts)
                if attempt < attempts:
                    time.sleep(min(backoff_delay(attempt), config.max_retry_wait))
                continue
            except PlaywrightError as exc:
                if attempt < attempts and _is_transient_network_error(exc):
                    delay = min(backoff_delay(attempt), config.max_retry_wait)
                    logger.warning(
                        "Navigation failed (%s); retrying in %.0fs (attempt %d/%d)",
                        exc,
                        delay,
                        attempt,
                        attempts,
                    )
                    time.sleep(delay)
                    continue
                raise ScrapeError(f"Navigation to {url} failed: {exc}") from exc

            status = response.status if response is not None else None
            if status in RETRYABLE_STATUS and attempt < attempts:
                headers = dict(response.headers) if response is not None else {}
                delay = _retry_delay(headers, attempt, config)
                logger.warning(
                    "Ground News returned HTTP %d; backing off %.0fs (attempt %d/%d)",
                    status,
                    delay,
                    attempt,
                    attempts,
                )
                time.sleep(delay)
                continue
            if status is not None and status >= 400:
                raise ScrapeError(f"Ground News returned HTTP {status} for {url}")
            return

        raise ScrapeError(
            f"Navigation to {url} timed out after {attempts} attempts"
        ) from last_timeout

    def _navigate(self, url: str) -> None:
        """Navigate to an article page and wait for its content."""
        config = self.config
        self._load(url)

        try:
            self.page.wait_for_selector("#titleArticle", state="visible", timeout=config.page_timeout_ms)
        except PlaywrightTimeoutError as exc:
            raise ScrapeError(
                "The article title never rendered. Ground News may be blocking this client, "
                f"showing a login/paywall, or the URL may be invalid (page title: {self.page.title()!r})."
            ) from exc

        try:
            self.page.wait_for_selector(SELECTOR_TOTAL_SOURCES[0], state="visible", timeout=10_000)
        except PlaywrightTimeoutError:
            logger.warning("The coverage breakdown did not appear; its counts will be empty")

        dismiss_blocking_dialogs(self.page)


def _route_handler(route) -> None:  # pragma: no cover - exercised by the browser only
    try:
        if route.request.resource_type in BLOCKED_RESOURCE_TYPES:
            route.abort()
        else:
            route.continue_()
    except PlaywrightError:
        pass
