"""Optional wall-clock instrumentation, for finding out where a run goes.

Off unless ``fetchit run --timings`` turns it on, and when it is off a
:func:`step` costs one boolean check — so the call sites can sit permanently in
the hot paths (every ``yt-dlp`` invocation, every download, every feed's
parse) without anyone paying for them.

Two kinds of output, both on stderr so they never mix into what a run actually
prints. Each step reports as it finishes, which is what you want while watching a
slow run go by; :func:`report` then totals them by category at the end, which is
what you want afterwards, because "38s in yt-dlp" is the sentence that decides
what to optimise and no individual line ever says it.

Steps are timed from several threads at once (see :func:`fetchit.run.run`),
so the concurrent total can exceed the wall-clock total. That is not a bug in the
arithmetic — it is the parallelism working, and comparing the two is the quickest
way to see how much of it there is.
"""

from __future__ import annotations

import sys
import threading
import time
from collections import defaultdict
from contextlib import contextmanager
from typing import Iterator

_enabled = False
_lock = threading.Lock()
_totals: defaultdict[str, float] = defaultdict(float)
_counts: defaultdict[str, int] = defaultdict(int)
_started: float | None = None


def enable() -> None:
    """Turn timing on for the rest of the process, and start the run clock."""
    global _enabled, _started
    with _lock:
        _enabled = True
        _started = time.perf_counter()
        _totals.clear()
        _counts.clear()


def enabled() -> bool:
    """Whether timings are being collected."""
    return _enabled


@contextmanager
def step(category: str, detail: str = "") -> Iterator[None]:
    """Time the block, filing it under ``category``.

    ``category`` is what the end-of-run summary groups on, so it should name a
    kind of work — ``"yt-dlp"``, ``"http download"``, ``"feedparser"`` — and
    ``detail`` the particular one, which only the per-step line shows.

    The block is timed whether or not it raises: work that failed still took the
    time, and a run that got slow because something is timing out is exactly the
    case this has to be able to show.
    """
    if not _enabled:
        yield
        return

    start = time.perf_counter()
    try:
        yield
    finally:
        elapsed = time.perf_counter() - start
        with _lock:
            _totals[category] += elapsed
            _counts[category] += 1
            label = f"{category}: {detail}" if detail else category
            print(f"[timing] {elapsed:7.2f}s  {label}", file=sys.stderr)


def report() -> None:
    """Print the by-category summary and the run total. No-op when disabled."""
    if not _enabled:
        return

    with _lock:
        totals = sorted(_totals.items(), key=lambda kv: kv[1], reverse=True)
        counts = dict(_counts)
        started = _started

    print("[timing] ---", file=sys.stderr)
    for category, total in totals:
        n = counts[category]
        print(
            f"[timing] {total:7.2f}s  {category} ({n} call{'s' if n != 1 else ''})",
            file=sys.stderr,
        )
    if started is not None:
        print(
            f"[timing] {time.perf_counter() - started:7.2f}s  run total (wall clock)",
            file=sys.stderr,
        )
