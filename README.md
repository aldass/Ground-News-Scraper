# Ground News Scraper 🌍📰

A web scraper for extracting comprehensive news data from [Ground.news](https://ground.news) articles, designed for media bias analysis and research purposes. Browser automation uses **Playwright** (Chromium), with request pacing built in so a batch run does not get the client rate-limited or banned.

![Python](https://img.shields.io/badge/python-3.12+-blue.svg)
![License](https://img.shields.io/badge/license-MIT-green.svg)
![Status](https://img.shields.io/badge/status-active-success.svg)

## 🎯 Overview

The scraper extracts, for each Ground News story:

- **Political bias distribution** across news sources
- **Perspective summaries** from left, center, and right viewpoints
- **Source metadata** with bias classifications
- **Article text** for every cited source, extracted with `trafilatura`

## ✨ Features

### 🔍 Data extraction
- **Multi-perspective analysis**: left, center, and right viewpoint summaries
- **Source tracking**: every outlet covering the story, including its bias rating
- **Full article text**: downloaded from each cited outlet and extracted into the JSON
- **Dynamic loading**: clicks "load more stories" until the source list is complete
- **Overlay handling**: dismisses the onboarding/subscribe dialog that otherwise blocks clicks

### 🚦 Polite by default
- **Request pacing shared across processes**: a state file and lock make the wait apply to every scraper process on the machine, including the `gn.py` batch runner
- **Randomised intervals**: per-host minimum interval plus jitter, so traffic is not machine-regular
- **Throttle-aware retries**: `429`/`5xx` responses back off exponentially and honour `Retry-After`
- **Optional `robots.txt` checks** for cited outlets (`--respect-robots`)
- **Sequential only**: one browser context, one request at a time

### 🖥️ Interfaces
- **GUI application**: tkinter interface with live console output
- **Command line**: one or more URLs per run, with configurable pacing
- **Optional login**: reuse a Ground News session through a saved Playwright storage state

### 📊 Data management
- **JSON storage**: one file per story, written atomically
- **Unique story IDs**: timestamp plus random suffix
- **Analysis helpers**: scripts for summarising and exporting the collected data

## 🏗️ Project structure

```
Ground-News-Scraper/
├── scraper.py          # Playwright scraper and extraction helpers
├── rate_limit.py       # Cross-process request pacing and backoff
├── g.py                # Command-line entry point (used by the GUI and gn.py)
├── login.py            # Saves a Ground News session for authenticated runs
├── gui.py              # Tkinter interface
├── gn.py               # Batch runner over a list of URLs
├── a.py                # Bias/text coverage analysis
├── asm.py              # Flat CSV export of all sources
├── csv_make.py         # Story/source/perspective CSV export
├── tests/              # pytest suite (rate limiting, extraction, schema)
├── json/               # Story JSON output (default)
├── state/              # Rate limit state (created at runtime, gitignored)
└── auth/               # Saved session (created by login.py, gitignored)
```

## 🚀 Quick start

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
uv run playwright install chromium
```

`uv sync` installs Playwright, trafilatura, tqdm, and the test dependencies. The second command downloads Chromium once; it is required by both the scraper and `login.py`.

### Run the GUI

```bash
uv run python gui.py
```

### Run the command line

```bash
uv run python g.py "https://ground.news/article/your-article-url"
```

## 💻 Usage

### Command line

```bash
# One URL
uv run python g.py "https://ground.news/article/..."

# Several URLs in one browser session (pacing still applies between them)
uv run python g.py URL1 URL2 URL3

# Watch the browser, and slow down further
uv run python g.py URL --headed --min-interval 8 --jitter 3

# Skip downloading full text for each cited source
uv run python g.py URL --no-source-text
```

Useful options (`uv run python g.py --help` lists them all):

| Option | Default | Meaning |
| --- | --- | --- |
| `--feed URL` | — | Topic/feed page whose article links are scraped (repeatable) |
| `--limit N` | `0` (all) | Articles to take from each `--feed` page |
| `--output-dir PATH` | `json/` next to the scripts | Where story JSON files are written |
| `--headed` | off | Show the browser window instead of running headless |
| `--min-interval SECONDS` | `4.0` | Minimum delay between requests to the same host |
| `--jitter SECONDS` | `2.0` | Random extra delay added to `--min-interval` |
| `--source-interval SECONDS` | `1.5` | Minimum delay between any two cited-article fetches |
| `--max-load-more BATCHES` | `40` | Cap on "load more stories" clicks per article |
| `--no-source-text` | off | Do not download cited articles |
| `--respect-robots` | off | Skip cited articles whose `robots.txt` disallows fetching |
| `--block-resources` | off | Block images, fonts, and media |
| `--timeout SECONDS` | `60` | Per-page navigation timeout |
| `--retries N` | `3` | Retries after a failed navigation or HTTP/network error |
| `--no-auth` | off | Ignore any saved session |
| `-v`, `--verbose` | off | Log debug details, including pacing waits |

The process exits with status `1` if any URL failed, so the GUI and batch runner can report failures.

### Topic feeds

A topic page such as `https://ground.news/interest/stock-markets` is a feed, not an article, so it cannot be passed as a plain URL. Use `--feed`:

```bash
# Read the feed and scrape up to 5 of the stories it lists
uv run python g.py --feed "https://ground.news/interest/stock-markets" --limit 5

# Cheap discovery pass: no per-source article text
uv run python g.py --feed "https://ground.news/interest/stock-markets" --limit 5 --no-source-text
```

The scraper opens the feed, collects its `/article/` links, de-duplicates them, and then scrapes each one with the normal pipeline and pacing. Only the links present on the first page load are collected; the feed is not scrolled or paginated. `--limit 0` (the default) scrapes every link found, which can take hours with source text enabled.

### Optional: use your Ground News account

Anonymous scraping works, but Ground News shows an onboarding/subscribe dialog and may limit some content for logged-out visitors. To reuse your session:

```bash
uv run python login.py
```

A browser window opens. Log in there (including two-factor), return to the terminal, and press Enter. The script writes `auth/storage_state.json`; `g.py` picks it up automatically on later runs. You never give this code your password: it stores the browser session, so treat the file like a password. It is gitignored. Delete it to log out.

### Batch runs

`gn.py` iterates a list of URLs and runs `g.py` once per URL. Because pacing state lives in `state/rate_limit.json`, the delay applies across those processes too.

### Data analysis

```bash
uv run python a.py        # bias/source summary
uv run python asm.py      # flat asm.csv of every source
uv run python csv_make.py # dataset/*.csv exports
```

## 📋 Data structure

Each scraped article generates a JSON file:

```json
{
  "story_id": "GN_20250820_213302_abc12345",
  "metadata": {
    "title": "Article Title",
    "timestamp": "2025-08-20T21:33:02.620020",
    "url": "https://ground.news/article/..."
  },
  "bias_distribution": {
    "total_sources": "105",
    "leaning_left": "40",
    "center": "27",
    "leaning_right": "13"
  },
  "perspective_summaries": {
    "left": ["Left perspective point 1", "..."],
    "center": ["Center perspective point 1", "..."],
    "right": ["Right perspective point 1", "..."]
  },
  "sources": [
    {
      "news_title": "Source Article Title",
      "news_link": "https://source-url.com/story",
      "bias": "Lean Right",
      "text": "Extracted article text, or null when the outlet could not be read"
    }
  ]
}
```

`text` is `null` when the outlet blocks the request (403), returns a non-HTML document, or `--no-source-text` is used. One unreachable source does not abort the story.

## 🔧 Technical details

### Playwright instead of Selenium

- One browser and context are reused for the whole run; each URL is a fresh navigation.
- Waits are event-driven (`wait_for_selector`) rather than fixed sleeps.
- Clicks go through Playwright's actionability checks, with an automatic retry after dismissing a late overlay.
- The user agent matches the Chromium build Playwright launched, and `navigator.webdriver` is masked.
- The extractor uses the same absolute XPaths the Selenium version used, because they still match the live site. The headline and source-card selectors have semantic fallbacks; the four coverage selectors remain single absolute XPaths, because no reliable semantic equivalent was found.

### Rate limiting

`rate_limit.FileRateLimiter` stores the next allowed request time per key in `state/rate_limit.json` and protects it with an OS file lock (`state/rate_limit.json.lock`):

- **Per host**: every navigation and every cited-article fetch to a host waits `min_interval + random(0, jitter)` seconds. Defaults are 4–6 seconds.
- **Global floor**: cited-article fetches also share a 1.5–2.5 second floor, so a story with a hundred outlets cannot burst.
- **Backoff**: `429`, `500`, `502`, `503`, and `504` are retried with exponential backoff (30s, 60s, 120s…), using `Retry-After` when present and clamping any single sleep to 300 seconds. Transient network errors are retried the same way.
- **Fail safe**: if the lock cannot be acquired in time, the run aborts with a clear error rather than scraping unpaced.
- **Cross-process**: the batch runner spawns one process per URL; they all share the same state file, so the pacing is not defeated by process boundaries.

Ground News' `robots.txt` allows `User-Agent: *` on `/`, and the scraper only fetches article pages. `--respect-robots` additionally checks each cited outlet before downloading its text.

### Source text extraction

Cited articles are fetched with the browser's request context (same user agent, cookies, redirect handling), then parsed with `trafilatura`. Responses that are not HTML are skipped. Network failures are logged and recorded as `null` text.

## ⚠️ Important notes

### Ethical usage
- **Pacing is on by default**; raising it (or lowering it) is your responsibility.
- **`--respect-robots`** is available for outlets whose `robots.txt` disallows crawling.
- **Academic use**: intended for research; credit Ground.news as the source of the story data.
- **Account use**: automated access may conflict with Ground News' terms. A logged-in session is your account's responsibility; keep `auth/storage_state.json` private.

### Technical considerations
- **Chromium**: install it once with `uv run playwright install chromium`.
- **Network**: the scraper needs a stable connection; some outlets block automated requests regardless of pacing.
- **Selectors**: Ground News is a Next.js app whose DOM can change. If a field disappears, update the constants at the top of `scraper.py`.
- **Runtime**: pacing is the point. A story with ~100 sources can take several minutes.

## 🧪 Tests

```bash
uv run pytest
```

The suite covers the rate limiter (persistence, lock handling, stale locks, backoff, `Retry-After`), the JSON schema, and the extraction helpers against a local DOM fixture. The fixture tests skip automatically if Chromium is not installed.

## 🤝 Contributing

Contributions are welcome:

1. Fork the repository
2. Create a feature branch
3. Commit your changes
4. Open a pull request

## 👤 Contributors

- **aldass** ([@aldass](https://github.com/aldass)) — Playwright port, cross-process rate limiting, topic-feed support

## 📄 License

MIT. See the [LICENSE](LICENSE) file.

## 🙏 Acknowledgments

- **Ground.news** for the coverage and bias data
- **Playwright** for browser automation
- **trafilatura** for article text extraction
- **Python community** for the supporting libraries

---

*Use this tool responsibly and in accordance with Ground News' terms of service.*
