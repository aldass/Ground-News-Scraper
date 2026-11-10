"""Tests for the command-line surface."""

from __future__ import annotations

import pytest

import g


def test_cli_requires_a_target() -> None:
    with pytest.raises(SystemExit):
        g.main([])


def test_cli_accepts_a_feed_and_limit() -> None:
    args = g.build_parser().parse_args(
        ["--feed", "https://ground.news/interest/stock-markets", "--limit", "3"]
    )

    assert args.urls == []
    assert args.feed == ["https://ground.news/interest/stock-markets"]
    assert args.limit == 3


def test_cli_accepts_several_feeds() -> None:
    args = g.build_parser().parse_args(
        ["--feed", "https://ground.news/a", "https://ground.news/b"]
    )

    assert args.feed == ["https://ground.news/a", "https://ground.news/b"]


def test_cli_rejects_a_negative_limit() -> None:
    with pytest.raises(SystemExit):
        g.main(["--feed", "https://ground.news/interest/stock-markets", "--limit", "-1"])


def test_cli_article_urls_are_still_positional() -> None:
    args = g.build_parser().parse_args(
        ["https://ground.news/article/one", "https://ground.news/article/two"]
    )

    assert args.urls == [
        "https://ground.news/article/one",
        "https://ground.news/article/two",
    ]
    assert args.feed == []
