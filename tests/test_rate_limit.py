"""Tests for the cross-process request pacing."""

from __future__ import annotations

import json
import random
from datetime import datetime, timezone
from pathlib import Path

import pytest

from rate_limit import (
    GLOBAL_KEY,
    FileRateLimiter,
    MemoryRateLimiter,
    RateLimitError,
    backoff_delay,
    host_key,
    parse_retry_after,
)


class FakeClock:
    """Deterministic clock whose sleep advances time instead of blocking."""

    def __init__(self, start: float = 1_000.0) -> None:
        self.now = start
        self.sleeps: list[float] = []

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def make_limiter(state_path: Path, clock: FakeClock, **kwargs) -> FileRateLimiter:
    return FileRateLimiter(
        state_path,
        clock=clock.time,
        monotonic=clock.time,
        sleep=clock.sleep,
        rng=random.Random(0),
        **kwargs,
    )


def test_first_request_does_not_wait(tmp_path: Path) -> None:
    clock = FakeClock()
    limiter = make_limiter(tmp_path / "state.json", clock)

    waited = limiter.wait("ground.news", 4.0, 2.0)

    assert waited == 0.0
    assert clock.sleeps == []


def test_same_key_waits_at_least_min_interval(tmp_path: Path) -> None:
    clock = FakeClock()
    limiter = make_limiter(tmp_path / "state.json", clock)
    limiter.wait("ground.news", 4.0, 2.0)

    started = clock.now
    limiter.wait("ground.news", 4.0, 2.0)
    elapsed = clock.now - started

    assert 4.0 <= elapsed <= 6.0


def test_different_keys_are_paced_independently(tmp_path: Path) -> None:
    clock = FakeClock()
    limiter = make_limiter(tmp_path / "state.json", clock)

    limiter.wait("ground.news", 4.0, 2.0)
    waited = limiter.wait("example.com", 4.0, 2.0)

    assert waited == 0.0
    assert clock.sleeps == []


def test_state_is_shared_between_limiter_instances(tmp_path: Path) -> None:
    """Simulates two scraper processes: the second must honour the first."""
    state = tmp_path / "state.json"
    clock = FakeClock()

    make_limiter(state, clock).wait("ground.news", 4.0, 2.0)
    waited = make_limiter(state, clock).wait("ground.news", 4.0, 2.0)

    assert waited >= 4.0


def test_state_file_records_the_request_time(tmp_path: Path) -> None:
    state = tmp_path / "state.json"
    clock = FakeClock()
    limiter = make_limiter(state, clock)

    limiter.wait("ground.news", 4.0, 0.0)

    data = json.loads(state.read_text(encoding="utf-8"))
    assert data["ground.news"] == pytest.approx(clock.now)


def test_corrupt_state_is_ignored(tmp_path: Path) -> None:
    state = tmp_path / "state.json"
    state.write_text("{not json", encoding="utf-8")
    clock = FakeClock()

    limiter = make_limiter(state, clock)
    assert limiter.wait("ground.news", 4.0, 0.0) == 0.0

    assert "ground.news" in json.loads(state.read_text(encoding="utf-8"))


def test_stale_stall_is_shared_when_the_global_floor_applies(tmp_path: Path) -> None:
    """A different host must still wait for the global source floor."""
    clock = FakeClock()
    limiter = make_limiter(tmp_path / "state.json", clock)

    limiter.wait_many([(GLOBAL_KEY, 1.5, 0.0), ("a.example", 4.0, 0.0)])
    started = clock.now
    waited = limiter.wait_many([(GLOBAL_KEY, 1.5, 0.0), ("b.example", 4.0, 0.0)])

    assert clock.now - started == pytest.approx(1.5)
    assert waited == pytest.approx(1.5)


def test_wait_many_takes_the_later_of_two_floors(tmp_path: Path) -> None:
    clock = FakeClock()
    limiter = make_limiter(tmp_path / "state.json", clock)

    limiter.wait_many([(GLOBAL_KEY, 1.5, 0.0), ("a.example", 4.0, 0.0)])
    started = clock.now
    limiter.wait_many([(GLOBAL_KEY, 1.5, 0.0), ("a.example", 4.0, 0.0)])

    assert clock.now - started == pytest.approx(4.0)


def test_lock_timeout_reports_the_running_scraper(tmp_path: Path) -> None:
    state = tmp_path / "state.json"
    clock = FakeClock()
    holder = make_limiter(state, clock, lock_timeout=5.0)
    waiter = make_limiter(state, clock, lock_timeout=1.0)

    with holder._locked():
        with pytest.raises(RateLimitError, match="Timed out waiting"):
            waiter.wait("ground.news", 0.0, 0.0)


def test_leftover_lock_file_is_reused_after_a_crashed_run(tmp_path: Path) -> None:
    """The OS drops the lock when a process dies, so the file itself is harmless."""
    state = tmp_path / "state.json"
    lock = state.with_name(state.name + ".lock")
    lock.write_text("999\n", encoding="utf-8")

    clock = FakeClock()
    assert make_limiter(state, clock).wait("ground.news", 4.0, 0.0) == 0.0


def test_wait_is_capped_when_clock_moves_backwards(tmp_path: Path) -> None:
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"ground.news": 10_000.0}), encoding="utf-8")

    clock = FakeClock(start=1_000.0)
    limiter = make_limiter(state, clock, max_wait=30.0)

    assert limiter.wait("ground.news", 4.0, 0.0) == 30.0


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("7", 7.0),
        (" 0 ", 0.0),
        ("-3", 0.0),
        ("not-a-date", None),
        ("", None),
        (None, None),
    ],
)
def test_parse_retry_after_seconds(value: str | None, expected: float | None) -> None:
    assert parse_retry_after(value) == expected


def test_parse_retry_after_http_date() -> None:
    now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    assert parse_retry_after("Thu, 01 Jan 2026 12:00:30 GMT", now=now) == 30.0


def test_parse_retry_after_past_date_is_zero() -> None:
    now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    assert parse_retry_after("Thu, 01 Jan 2026 11:00:00 GMT", now=now) == 0.0


def test_backoff_grows_and_stays_capped() -> None:
    delays = [backoff_delay(attempt, rng=random.Random(0)) for attempt in range(1, 8)]

    assert delays[0] < delays[1] < delays[2]
    assert all(delay <= 300.0 + 5.0 for delay in delays)


def test_backoff_rejects_attempt_zero() -> None:
    with pytest.raises(ValueError):
        backoff_delay(0)


def test_memory_limiter_paces_each_key() -> None:
    clock = FakeClock(start=0.0)
    limiter = MemoryRateLimiter(clock=clock.time, sleep=clock.sleep, rng=random.Random(1))

    limiter.wait("a", 3.0, 0.0)
    assert limiter.wait("b", 3.0, 0.0) == 0.0
    assert limiter.wait("a", 3.0, 0.0) == pytest.approx(3.0)


def test_memory_limiter_composes_a_global_floor() -> None:
    clock = FakeClock(start=0.0)
    limiter = MemoryRateLimiter(clock=clock.time, sleep=clock.sleep, rng=random.Random(1))

    limiter.wait_many([(GLOBAL_KEY, 2.0, 0.0), ("a", 5.0, 0.0)])
    assert limiter.wait_many([(GLOBAL_KEY, 2.0, 0.0), ("b", 5.0, 0.0)]) == pytest.approx(2.0)


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://ground.news/article/x_f8c282", "ground.news"),
        ("https://Example.COM/News", "example.com"),
        ("not a url", "unknown-host"),
        ("http://[", "unknown-host"),
    ],
)
def test_host_key(url: str, expected: str) -> None:
    assert host_key(url) == expected
