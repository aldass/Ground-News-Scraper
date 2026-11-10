"""Tests for the Playwright extraction helpers.

The browser tests run against a local HTML fixture shaped like the live
Ground News article DOM, so they never touch the network. They are skipped
when the Chromium binary is missing.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from urllib.robotparser import RobotFileParser

import pytest
from playwright.sync_api import Error as PlaywrightError, Page, sync_playwright

import scraper
from scraper import (
    GroundNewsScraper,
    RobotsPolicy,
    ScrapeConfig,
    ScrapeError,
    _parse_total,
    build_structured_data,
    collect_article_links,
    dismiss_blocking_dialogs,
    extract_article_data,
    extract_sources,
    fetch_article_text,
    load_all_stories,
    save_story,
)

FIXTURE_HTML = """
<!doctype html>
<html>
  <body>
    <main>
      <div>
        <article>
          <div>
            <div>
              <!-- div[1]: perspective panel, mirrors the live absolute XPaths -->
              <div>
                <button type="button" id="left-summary-button">Left</button>
                <button type="button" id="center-summary-button">Center</button>
                <button type="button" id="right-summary-button">Right</button>
                <div></div>
                <div>
                  <div></div>
                  <div></div>
                  <div>
                    <div>
                      <div>
                        <div></div>
                        <div>
                          <div>
                            <ul id="points"><li>default point</li></ul>
                          </div>
                        </div>
                      </div>
                    </div>
                  </div>
                </div>
              </div>

              <!-- div[2]: headline, source cards, load-more button -->
              <div>
                <h1 id="titleArticle">Fixture headline</h1>
                <div id="sources">
                  <div id="article-summary">
                    <div>
                      <a href="https://example.com/first"><h4>First headline</h4></a>
                      <a id="article-source-bias-a" href="/interest/a#bias-ratings">
                        <div>Lean Left</div>
                      </a>
                      <a id="article-source-info-a" href="/interest/a">Source A</a>
                    </div>
                  </div>
                  <div id="article-summary">
                    <div>
                      <a href="https://example.com/second"><h4>Second headline</h4></a>
                      <a id="article-source-bias-b" href="/interest/b#bias-ratings">
                        <div>Lean Right</div>
                      </a>
                      <a id="article-source-info-b" href="/interest/b">Source B</a>
                    </div>
                  </div>
                  <template id="extra-cards">
                    <div id="article-summary">
                      <div>
                        <a href="https://example.com/third"><h4>Third headline</h4></a>
                        <a id="article-source-bias-c" href="/interest/c#bias-ratings">
                          <div>Center</div>
                        </a>
                        <a id="article-source-info-c" href="/interest/c">Source C</a>
                      </div>
                    </div>
                  </template>
                </div>
                <button type="button" id="more-stories">More stories</button>
              </div>

              <!-- div[3] -->
              <div></div>

              <!-- div[4]: coverage breakdown, mirrors the live absolute XPaths -->
              <div>
                <div>
                  <div>
                    <h3>Coverage Details</h3>
                    <div><span>Total News Sources</span><span>105</span></div>
                    <span>Leaning Left</span><span>40</span>
                    <span>Leaning Right</span><span>13</span>
                    <span>Center</span><span>27</span>
                  </div>
                </div>
              </div>
            </div>
          </div>
        </article>
      </div>
    </main>
  </body>
</html>
"""

FIXTURE_SCRIPT = """
() => {
  const points = {
    left: ['Left one', 'Left two'],
    center: ['Center one'],
    right: ['Right one', 'Right two', 'Right three'],
  };
  for (const [side, items] of Object.entries(points)) {
    document.getElementById(side + '-summary-button').addEventListener('click', () => {
      document.getElementById('points').innerHTML =
        items.map((item) => '<li>' + item + '</li>').join('');
    });
  }
  let batches = 0;
  document.getElementById('more-stories').addEventListener('click', () => {
    batches += 1;
    const button = document.getElementById('more-stories');
    if (batches > 1) {
      button.remove();
      return;
    }
    document.getElementById('sources').insertAdjacentHTML(
      'beforeend',
      document.getElementById('extra-cards').innerHTML,
    );
  });
}
"""


@pytest.fixture(scope="session")
def browser():
    with sync_playwright() as playwright:
        try:
            instance = playwright.chromium.launch(headless=True)
        except PlaywrightError as exc:  # pragma: no cover - depends on the machine
            pytest.skip(f"Chromium is not installed: {exc}")
        try:
            yield instance
        finally:
            instance.close()


@pytest.fixture
def page(browser) -> Page:
    context = browser.new_context(viewport={"width": 1440, "height": 900})
    page = context.new_page()
    page.set_content(FIXTURE_HTML)
    page.evaluate(FIXTURE_SCRIPT)
    try:
        yield page
    finally:
        context.close()


def test_extract_article_data_reads_the_coverage_breakdown(page: Page) -> None:
    data = extract_article_data(page, summary_settle_ms=1_500)

    assert data["title"] == "Fixture headline"
    assert data["total_source"] == "105"
    assert data["leaning_left"] == "40"
    assert data["leaning_right"] == "13"
    assert data["center"] == "27"


def test_extract_article_data_switches_perspectives(page: Page) -> None:
    data = extract_article_data(page, summary_settle_ms=1_500)

    assert data["left_points"] == ["Left one", "Left two"]
    assert data["center_points"] == ["Center one"]
    assert data["right_points"] == ["Right one", "Right two", "Right three"]


def test_extract_article_data_without_a_title_raises(browser) -> None:
    page = browser.new_page()
    page.set_content("<html><body><p>Not an article</p></body></html>")
    with pytest.raises(ScrapeError, match="title"):
        extract_article_data(page)
    page.close()


def test_extract_sources_reads_title_link_and_bias(page: Page) -> None:
    sources = extract_sources(page)

    assert sources == [
        {
            "news_title": "First headline",
            "news_link": "https://example.com/first",
            "bias": "Lean Left",
            "text": None,
        },
        {
            "news_title": "Second headline",
            "news_link": "https://example.com/second",
            "bias": "Lean Right",
            "text": None,
        },
    ]


def test_extract_sources_finds_the_article_when_the_headline_is_not_a_link(browser) -> None:
    page = browser.new_page()
    page.set_content(
        '<div id="article-summary"><div>'
        '<a href="/interest/example">Example</a>'
        "<h4>Headline outside the anchor</h4>"
        '<a href="https://news.example/story">Read the story</a>'
        '<a id="article-source-bias-z"><div>Center</div></a>'
        "</div></div>"
    )

    sources = extract_sources(page)

    assert sources == [
        {
            "news_title": "Headline outside the anchor",
            "news_link": "https://news.example/story",
            "bias": "Center",
            "text": None,
        }
    ]
    page.close()


def test_extract_sources_ignores_ground_news_links_inside_a_card(browser) -> None:
    page = browser.new_page()
    page.set_content(
        '<div id="article-summary"><div>'
        '<a href="https://ground.news/article/x">On Ground News</a>'
        '<a href="https://news.example/story"><h4>Real headline</h4></a>'
        "</div></div>"
    )

    sources = extract_sources(page)

    assert sources[0]["news_link"] == "https://news.example/story"
    page.close()


def test_extract_sources_skips_share_links(browser) -> None:
    page = browser.new_page()
    page.set_content(
        '<div id="article-summary"><div>'
        '<a href="https://twitter.com/intent/tweet?url=https://news.example/story">Share</a>'
        "<h4>Headline</h4>"
        '<a href="https://news.example/story">Read the story</a>'
        "</div></div>"
    )

    sources = extract_sources(page)

    assert sources[0]["news_link"] == "https://news.example/story"
    page.close()


def test_extract_sources_skips_a_card_with_a_malformed_link(browser) -> None:
    page = browser.new_page()
    page.set_content(
        '<div id="article-summary"><div>'
        '<a href="http://["><h4>Broken link</h4></a>'
        "</div></div>"
    )

    assert extract_sources(page) == []
    page.close()


def test_extract_sources_uses_the_text_fetcher(page: Page) -> None:
    requested: list[str] = []

    def fetch_text(link: str) -> str:
        requested.append(link)
        return f"text for {link}"

    sources = extract_sources(page, fetch_text=fetch_text)

    assert requested == ["https://example.com/first", "https://example.com/second"]
    assert sources[0]["text"] == "text for https://example.com/first"


def test_load_all_stories_clicks_until_the_button_disappears(page: Page) -> None:
    config = ScrapeConfig(max_load_more_batches=5, load_more_settle_ms=2_000)

    count = load_all_stories(page, config=config)

    assert count == 3
    assert page.locator("#article-summary").count() == 3
    assert page.locator("#more-stories").count() == 0


def test_load_all_stories_stops_at_the_configured_limit(page: Page) -> None:
    config = ScrapeConfig(max_load_more_batches=0, load_more_settle_ms=2_000)

    assert load_all_stories(page, config=config) == 2


def test_extract_sources_after_loading_more_includes_new_cards(page: Page) -> None:
    config = ScrapeConfig(max_load_more_batches=5, load_more_settle_ms=2_000)
    load_all_stories(page, config=config)

    sources = extract_sources(page)

    assert [source["news_link"] for source in sources] == [
        "https://example.com/first",
        "https://example.com/second",
        "https://example.com/third",
    ]


def test_a_perspective_that_does_not_change_is_not_duplicated(page: Page) -> None:
    # Strip the center listener: clicking center must leave the previous list in place.
    page.evaluate(
        "() => { const b = document.getElementById('center-summary-button');"
        " b.replaceWith(b.cloneNode(true)); }"
    )

    data = extract_article_data(page, summary_settle_ms=800)

    assert data["left_points"] == ["Left one", "Left two"]
    assert data["center_points"] == []
    assert data["right_points"] == ["Right one", "Right two", "Right three"]


def test_left_perspective_is_kept_when_it_is_already_the_default(page: Page) -> None:
    page.evaluate(
        "document.getElementById('points').innerHTML = "
        "'<li>Left one</li><li>Left two</li>'"
    )

    data = extract_article_data(page, summary_settle_ms=800)

    assert data["left_points"] == ["Left one", "Left two"]
    assert data["center_points"] == ["Center one"]


def test_dismiss_blocking_dialogs_closes_the_onboarding_dialog(browser) -> None:
    page = browser.new_page()
    page.set_content(
        '<div data-testid="onboarding-dialog">'
        '<button data-testid="onboarding-close-button" '
        "onclick=\"this.closest('[data-testid=onboarding-dialog]').remove()\">Proceed</button>"
        "</div>"
    )

    assert dismiss_blocking_dialogs(page) is True
    assert page.locator('[data-testid="onboarding-dialog"]').count() == 0
    page.close()


def test_dismiss_blocking_dialogs_closes_a_generic_dialog(browser) -> None:
    page = browser.new_page()
    page.set_content(
        '<div id="modal"><button data-slot="dialog-close" '
        "onclick=\"document.getElementById('modal').remove()\">Close</button></div>"
    )

    assert dismiss_blocking_dialogs(page) is True
    assert page.locator("#modal").count() == 0
    page.close()


def test_collect_article_links_normalises_and_dedupes(browser) -> None:
    page = browser.new_page()
    page.set_content(
        '<a href="/article/one_hash">1</a>'
        '<a href="/article/one_hash">duplicate</a>'
        '<a href="https://ground.news/article/two_hash?utm_source=x">2</a>'
        '<a href="/article/three_hash#top">3</a>'
        '<a href="/interest/stock-markets">topic</a>'
        '<a href="https://example.com/article/other">external</a>'
        '<a href="http://[">malformed</a>'
    )

    assert collect_article_links(page) == [
        "https://ground.news/article/one_hash",
        "https://ground.news/article/two_hash",
        "https://ground.news/article/three_hash",
    ]
    page.close()


def test_collect_article_links_respects_the_limit(browser) -> None:
    page = browser.new_page()
    page.set_content(
        '<a href="/article/one">1</a>'
        '<a href="/article/two">2</a>'
        '<a href="/article/three">3</a>'
    )

    assert collect_article_links(page, limit=2) == [
        "https://ground.news/article/one",
        "https://ground.news/article/two",
    ]
    page.close()


def test_build_structured_data_keeps_the_existing_schema() -> None:
    article = {
        "title": "A headline",
        "total_source": "105",
        "leaning_left": "40",
        "leaning_right": "13",
        "center": "27",
        "left_points": ["left"],
        "center_points": ["center"],
        "right_points": ["right"],
        "sources": [{"news_title": "t", "news_link": "l", "bias": "b", "text": "x"}],
    }

    story = build_structured_data(
        "https://ground.news/article/x",
        article,
        story_id="GN_fixed",
        timestamp=datetime(2026, 1, 2, 3, 4, 5),
    )

    assert story == {
        "story_id": "GN_fixed",
        "metadata": {
            "title": "A headline",
            "timestamp": "2026-01-02T03:04:05",
            "url": "https://ground.news/article/x",
        },
        "bias_distribution": {
            "total_sources": "105",
            "leaning_left": "40",
            "center": "27",
            "leaning_right": "13",
        },
        "perspective_summaries": {
            "left": ["left"],
            "center": ["center"],
            "right": ["right"],
        },
        "sources": [{"news_title": "t", "news_link": "l", "bias": "b", "text": "x"}],
    }


def test_save_story_writes_pretty_json_and_leaves_no_temp_file(tmp_path: Path) -> None:
    story = build_structured_data(
        "https://ground.news/article/x",
        {"title": "T", "sources": []},
        story_id="GN_test",
        timestamp=datetime(2026, 1, 2, 3, 4, 5),
    )

    path = save_story(story, tmp_path)

    assert path == tmp_path / "GN_test.json"
    assert json.loads(path.read_text(encoding="utf-8"))["metadata"]["title"] == "T"
    assert list(tmp_path.iterdir()) == [path]


@pytest.mark.parametrize(
    ("value", "expected"),
    [("105", 105), ("1,234", 1234), ("", 0), ("unknown", 0)],
)
def test_parse_total(value: str, expected: int) -> None:
    assert _parse_total(value) == expected


class FakeResponse:
    def __init__(self, status: int, body: str = "", headers: dict | None = None) -> None:
        self.status = status
        self._body = body
        self.headers = headers or {}
        self.disposed = False

    def text(self) -> str:
        return self._body

    def dispose(self) -> None:
        self.disposed = True


class FakeRequest:
    def __init__(self, responses: list) -> None:
        self._responses = list(responses)
        self.urls: list[str] = []

    def get(self, url: str, **kwargs):
        self.urls.append(url)
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class FakeContext:
    def __init__(self, *responses: FakeResponse) -> None:
        self.responses = list(responses)
        self.request = FakeRequest(self.responses)


def test_fetch_article_text_retries_after_429_and_honours_retry_after(monkeypatch) -> None:
    html = "<html><body><article><p>Body</p></article></body></html>"
    context = FakeContext(
        FakeResponse(429, headers={"retry-after": "7"}),
        FakeResponse(200, html, {"content-type": "text/html; charset=utf-8"}),
    )
    sleeps: list[float] = []
    monkeypatch.setattr(scraper.time, "sleep", lambda seconds: sleeps.append(seconds))
    monkeypatch.setattr(
        scraper.trafilatura, "extract", lambda data, url=None: f"extracted:{len(data)}"
    )

    text = fetch_article_text(context, "https://news.example/story", config=ScrapeConfig())

    assert text == f"extracted:{len(html)}"
    assert sleeps == [7.0]
    assert context.request.urls == ["https://news.example/story", "https://news.example/story"]
    assert all(response.disposed for response in context.responses)


def test_fetch_article_text_skips_non_html_responses() -> None:
    context = FakeContext(FakeResponse(200, "%PDF-1.4", {"content-type": "application/pdf"}))

    assert fetch_article_text(context, "https://news.example/file.pdf", config=ScrapeConfig()) is None
    assert context.responses[0].disposed


def test_fetch_article_text_honours_retry_after_zero(monkeypatch) -> None:
    html = "<html><body><p>Body</p></body></html>"
    context = FakeContext(
        FakeResponse(429, headers={"retry-after": "0"}),
        FakeResponse(200, html, {"content-type": "text/html"}),
    )
    sleeps: list[float] = []
    monkeypatch.setattr(scraper.time, "sleep", lambda seconds: sleeps.append(seconds))
    monkeypatch.setattr(scraper.trafilatura, "extract", lambda data, url=None: "ok")

    assert fetch_article_text(context, "https://news.example/story", config=ScrapeConfig()) == "ok"
    assert sleeps == [0.0]


def test_fetch_article_text_clamps_an_oversized_retry_after(monkeypatch) -> None:
    html = "<html><body><p>Body</p></body></html>"
    context = FakeContext(
        FakeResponse(429, headers={"retry-after": "86400"}),
        FakeResponse(200, html, {"content-type": "text/html"}),
    )
    sleeps: list[float] = []
    monkeypatch.setattr(scraper.time, "sleep", lambda seconds: sleeps.append(seconds))
    monkeypatch.setattr(scraper.trafilatura, "extract", lambda data, url=None: "ok")

    config = ScrapeConfig(max_retry_wait=60.0)
    assert fetch_article_text(context, "https://news.example/story", config=config) == "ok"
    assert sleeps == [60.0]


def test_fetch_article_text_retries_a_transient_network_error(monkeypatch) -> None:
    html = "<html><body><p>Body</p></body></html>"
    context = FakeContext(
        PlaywrightError("net::ERR_CONNECTION_RESET"),
        FakeResponse(200, html, {"content-type": "text/html"}),
    )
    sleeps: list[float] = []
    monkeypatch.setattr(scraper.time, "sleep", lambda seconds: sleeps.append(seconds))
    monkeypatch.setattr(scraper.trafilatura, "extract", lambda data, url=None: "ok")

    assert fetch_article_text(context, "https://news.example/story", config=ScrapeConfig()) == "ok"
    assert 30.0 <= sleeps[0] <= 35.0
    assert len(context.request.urls) == 2


def test_fetch_article_text_does_not_retry_a_403() -> None:
    context = FakeContext(FakeResponse(403, "Forbidden", {"content-type": "text/html"}))

    assert fetch_article_text(context, "https://news.example/story", config=ScrapeConfig()) is None
    assert len(context.request.urls) == 1


def test_fetch_article_text_fails_fast_when_the_browser_is_closed(monkeypatch) -> None:
    context = FakeContext(PlaywrightError("Target page, context or browser has been closed"))
    sleeps: list[float] = []
    monkeypatch.setattr(scraper.time, "sleep", lambda seconds: sleeps.append(seconds))

    assert fetch_article_text(context, "https://news.example/story", config=ScrapeConfig()) is None
    assert sleeps == []
    assert len(context.request.urls) == 1


def test_fetch_article_text_gives_up_after_the_retry_budget(monkeypatch) -> None:
    context = FakeContext(*[FakeResponse(503) for _ in range(4)])
    sleeps: list[float] = []
    monkeypatch.setattr(scraper.time, "sleep", lambda seconds: sleeps.append(seconds))

    config = ScrapeConfig(max_retries=3)
    assert fetch_article_text(context, "https://news.example/story", config=config) is None
    assert len(sleeps) == 3
    assert len(context.request.urls) == 4


def test_fetch_article_text_skips_a_robots_disallowed_url() -> None:
    parser = RobotFileParser()
    parser.parse(["User-agent: *", "Disallow: /private"])
    robots = RobotsPolicy("Mozilla/5.0")
    robots._cache["news.example"] = parser
    context = FakeContext(FakeResponse(200, "never used", {"content-type": "text/html"}))

    assert (
        fetch_article_text(
            context,
            "https://news.example/private/story",
            config=ScrapeConfig(),
            robots=robots,
        )
        is None
    )
    assert context.request.urls == []


def test_robots_policy_allows_hosts_without_a_rule_file() -> None:
    robots = RobotsPolicy("Mozilla/5.0")
    robots._cache["news.example"] = None

    assert robots.allowed("https://news.example/story") is True


def test_scrape_config_defaults_keep_requests_paced() -> None:
    config = ScrapeConfig()

    assert config.min_interval >= 2.0
    assert config.source_interval >= 1.0
    assert config.max_retries >= 1
    assert config.fetch_source_text is True


def test_usable_storage_state_ignores_missing_and_corrupt_files(tmp_path: Path) -> None:
    assert scraper._usable_storage_state(None) is None
    assert scraper._usable_storage_state(tmp_path / "missing.json") is None

    corrupt = tmp_path / "corrupt.json"
    corrupt.write_text("{not json", encoding="utf-8")
    assert scraper._usable_storage_state(corrupt) is None

    not_a_state = tmp_path / "other.json"
    not_a_state.write_text(json.dumps({"foo": 1}), encoding="utf-8")
    assert scraper._usable_storage_state(not_a_state) is None

    valid = tmp_path / "valid.json"
    valid.write_text(json.dumps({"cookies": [], "origins": []}), encoding="utf-8")
    assert scraper._usable_storage_state(valid) == valid


def test_scraper_requires_a_context_manager() -> None:
    with pytest.raises(RuntimeError):
        _ = GroundNewsScraper(ScrapeConfig()).page
