"""Save a Ground News login session for the scraper.

Run this once:

    uv run python login.py

A browser window opens on ground.news. Log in normally, including any
two-factor step, then come back to the terminal and press Enter. The script
writes ``auth/storage_state.json``, and ``g.py`` picks it up automatically on
later runs.

The file contains session cookies: treat it like a password. It is gitignored
and never printed by any script here. Delete it to log out.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from playwright.sync_api import Error as PlaywrightError, sync_playwright

from scraper import DEFAULT_STORAGE_STATE, browser_user_agent


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Save a Ground News login session for the scraper.")
    parser.add_argument(
        "--storage-state",
        type=Path,
        default=DEFAULT_STORAGE_STATE,
        help=f"where to write the session file (default: {DEFAULT_STORAGE_STATE})",
    )
    parser.add_argument(
        "--url",
        default="https://ground.news/",
        help="page to open for logging in (default: https://ground.news/)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    target = Path(args.storage_state).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=False)
            context = browser.new_context(
                user_agent=browser_user_agent(browser),
                viewport={"width": 1440, "height": 900},
                locale="en-US",
            )
            page = context.new_page()
            page.goto(args.url, wait_until="domcontentloaded", timeout=60_000)

            print("A browser window is open on Ground News.")
            print("Log in there (2FA included), wait until you can see your account, then return here.")
            try:
                input("Press Enter to save the session... ")
            except (EOFError, KeyboardInterrupt):
                print("\nCancelled; nothing was saved.")
                browser.close()
                return 130

            # Pre-create the temp file with owner-only permissions so the cookie
            # jar is never readable by other local users, even briefly.
            try:
                os.close(os.open(tmp, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600))
                context.storage_state(path=str(tmp))
                try:
                    os.chmod(tmp, 0o600)
                except OSError:  # pragma: no cover - Windows ignores the mode
                    pass
                os.replace(tmp, target)
            except (PlaywrightError, OSError) as exc:
                try:
                    tmp.unlink()
                except OSError:
                    pass
                print(f"Could not write the session to {target}: {exc}", file=sys.stderr)
                browser.close()
                return 1
            browser.close()
    except PlaywrightError as exc:
        print(f"Playwright could not start: {exc}", file=sys.stderr)
        print("Install the browser once with: uv run playwright install chromium", file=sys.stderr)
        return 1

    print(f"Saved the session to {target}")
    print("Treat that file like a password: it grants access to your account.")
    print("The scraper now uses it automatically. Delete it to log out.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
