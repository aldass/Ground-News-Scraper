"""Request pacing for the Ground News scraper.

One article page can require dozens of requests: the initial navigation, a
click for every "load more stories" batch, and one fetch per cited source.
The batch helper (``gn.py``) runs one process per URL, so an in-memory limiter
would not see the other processes. This module therefore stores the next
allowed request time per key in a JSON file and protects it with an operating
system file lock, so pacing is shared by every scraper process on the machine.

A request slot is *reserved* under the lock and the sleep happens after the
lock is released. Holding the lock while sleeping would stall unrelated hosts
and turn a killed process into a long stall.

The state file is not secret; it only holds host names and timestamps.
"""

from __future__ import annotations

import json
import logging
import os
import random
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlsplit

logger = logging.getLogger("groundnews.rate_limit")

DEFAULT_STATE_PATH = Path(__file__).resolve().parent / "state" / "rate_limit.json"

# Status codes where stopping and retrying later is the correct response.
RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})

GLOBAL_KEY = "__global__"

# A request slot is a (key, minimum interval, jitter) triple.
Slot = tuple[str, float, float]


class RateLimitError(RuntimeError):
    """Raised when the shared pacing state cannot be read or locked.

    Continuing without pacing risks a ban, so callers should abort the run
    rather than scrape unpaced.
    """


def host_key(url: str) -> str:
    """Return the pacing key for a URL, i.e. its lower-case host."""
    try:
        host = urlsplit(url).netloc.lower()
    except ValueError:
        return "unknown-host"
    return host or "unknown-host"


def _lock_fd(fd: int) -> None:
    """Take an exclusive, non-blocking OS lock on the first byte of ``fd``."""
    if os.name == "nt":  # pragma: no cover - exercised on Windows
        import msvcrt

        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    else:  # pragma: no cover - exercised on POSIX
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock_fd(fd: int) -> None:
    if os.name == "nt":  # pragma: no cover - exercised on Windows
        import msvcrt

        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    else:  # pragma: no cover - exercised on POSIX
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_UN)


class FileRateLimiter:
    """Enforce a minimum interval between requests, across processes.

    ``wait`` reserves the next allowed time for a key, sleeps until then, and
    records it. Keys are paced independently, so unrelated news sites do not
    wait for each other; use :data:`GLOBAL_KEY` to add a floor across hosts, or
    ``wait_many`` to pace several keys as one request.

    The OS releases the lock when a process dies, so a killed run cannot strand
    the scraper behind a stale lock.

    ``clock``, ``sleep`` and ``rng`` are injectable so tests never really sleep.
    """

    def __init__(
        self,
        state_path: Path | str = DEFAULT_STATE_PATH,
        *,
        lock_timeout: float = 60.0,
        max_wait: float = 300.0,
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        rng: random.Random | None = None,
    ) -> None:
        self.state_path = Path(state_path)
        self.lock_path = self.state_path.with_name(self.state_path.name + ".lock")
        self.lock_timeout = lock_timeout
        self.max_wait = max_wait
        self._clock = clock
        self._monotonic = monotonic
        self._sleep = sleep
        self._rng = rng or random.Random()

    def wait(self, key: str, min_interval: float, jitter: float = 0.0) -> float:
        """Block until ``key`` may be requested again; return seconds waited."""
        return self.wait_many([(key, min_interval, jitter)])

    def wait_many(self, slots: Sequence[Slot]) -> float:
        """Reserve one request time for every slot, then sleep until it.

        All slots are recorded at the same instant, which is the moment the
        request actually happens. That makes a per-host interval and a global
        floor compose correctly instead of measuring the floor from before the
        per-host wait.
        """
        if not slots:
            return 0.0
        for _, min_interval, jitter in slots:
            if min_interval < 0 or jitter < 0:
                raise ValueError("min_interval and jitter must be non-negative")

        with self._locked():
            state = self._read_state()
            now = self._clock()
            target = self._target(state, slots, now)
            if target - now > self.max_wait:
                logger.warning(
                    "Rate limit state is %.0fs in the future; capping the wait at %.0fs",
                    target - now,
                    self.max_wait,
                )
                target = now + self.max_wait
            for key, _, _ in slots:
                state[key] = target
            self._write_state(state)

        delay = max(0.0, target - now)
        if delay:
            logger.debug("Pacing %s: waiting %.2fs", ", ".join(key for key, _, _ in slots), delay)
            self._sleep(delay)
        return delay

    def _target(self, state: dict[str, float], slots: Sequence[Slot], now: float) -> float:
        target = now
        for key, min_interval, jitter in slots:
            last = state.get(key)
            if last is None:
                continue
            candidate = last + min_interval + self._rng.uniform(0, jitter)
            if candidate > target:
                target = candidate
        return target

    @contextmanager
    def _locked(self) -> Iterator[None]:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(self.lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        except OSError as exc:
            raise RateLimitError(f"Cannot open lock file {self.lock_path}: {exc}") from exc

        deadline = self._monotonic() + self.lock_timeout
        try:
            while True:
                try:
                    _lock_fd(fd)
                    break
                except OSError:
                    if self._monotonic() >= deadline:
                        raise RateLimitError(
                            f"Timed out waiting for {self.lock_path} after {self.lock_timeout:.0f}s. "
                            "Another scraper may still be running."
                        ) from None
                    self._sleep(0.05)
            try:
                yield
            finally:
                try:
                    _unlock_fd(fd)
                except OSError:
                    pass
        finally:
            os.close(fd)

    def _read_state(self) -> dict[str, float]:
        try:
            raw = self.state_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}
        except OSError as exc:
            raise RateLimitError(f"Cannot read rate limit state {self.state_path}: {exc}") from exc

        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("Rate limit state %s is corrupt; starting from an empty state", self.state_path)
            return {}

        if not isinstance(data, dict):
            logger.warning("Rate limit state %s has an unexpected shape; ignoring it", self.state_path)
            return {}

        state: dict[str, float] = {}
        for key, value in data.items():
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            state[str(key)] = float(value)
        return state

    def _write_state(self, state: dict[str, float]) -> None:
        tmp_path = self.state_path.with_name(f"{self.state_path.name}.{os.getpid()}.tmp")
        try:
            with open(tmp_path, "w", encoding="utf-8") as handle:
                json.dump(state, handle, indent=0, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_path, self.state_path)
        except OSError as exc:
            try:
                tmp_path.unlink()
            except OSError:
                pass
            raise RateLimitError(f"Cannot write rate limit state {self.state_path}: {exc}") from exc


class MemoryRateLimiter:
    """Thread-safe in-memory limiter with the same reservation semantics.

    Used by tests and by single-process callers that do not want a state file.
    """

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        rng: random.Random | None = None,
    ) -> None:
        self._clock = clock
        self._sleep = sleep
        self._rng = rng or random.Random()
        self._last: dict[str, float] = {}
        self._lock = threading.Lock()

    def wait(self, key: str, min_interval: float, jitter: float = 0.0) -> float:
        return self.wait_many([(key, min_interval, jitter)])

    def wait_many(self, slots: Sequence[Slot]) -> float:
        if not slots:
            return 0.0
        for _, min_interval, jitter in slots:
            if min_interval < 0 or jitter < 0:
                raise ValueError("min_interval and jitter must be non-negative")

        with self._lock:
            now = self._clock()
            target = now
            for key, min_interval, jitter in slots:
                last = self._last.get(key)
                if last is None:
                    continue
                target = max(target, last + min_interval + self._rng.uniform(0, jitter))
            for key, _, _ in slots:
                self._last[key] = target

        delay = max(0.0, target - now)
        if delay:
            self._sleep(delay)
        return delay


def parse_retry_after(value: str | None, *, now: datetime | None = None) -> float | None:
    """Parse a ``Retry-After`` header (delta seconds or HTTP date) into seconds."""
    if not value:
        return None
    value = value.strip()
    if not value:
        return None

    try:
        seconds = float(value)
    except ValueError:
        pass
    else:
        return max(0.0, seconds)

    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when is None:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    reference = now or datetime.now(timezone.utc)
    return max(0.0, (when - reference).total_seconds())


def backoff_delay(
    attempt: int,
    *,
    base: float = 30.0,
    cap: float = 300.0,
    jitter: float = 5.0,
    rng: random.Random | None = None,
) -> float:
    """Exponential backoff for retry ``attempt`` (1-based), with jitter."""
    if attempt < 1:
        raise ValueError("attempt must be >= 1")
    rng = rng or random.Random()
    return min(cap, base * (2 ** (attempt - 1))) + rng.uniform(0, jitter)
