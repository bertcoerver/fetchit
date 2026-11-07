"""Orchestration: read every feed, work out what is new, download it, file it.

A run goes in two phases. Every feed is *read* at once — fetching a feed is
nearly all waiting on somebody else's server, and no feed's listing can affect
another's — and then each feed's new items are *downloaded* one at a time, in
the order they were published. Downloads stay sequential on purpose: a
Raspberry Pi's link is the bottleneck either way, Downloadgemist asks for a
pause between them, and order is a promise this phase makes.

The promise is that a feed's items reach the library oldest first, and that a
newer one is never filed while an older one is still owed. Whatever reads the
library by publication date — rssignal, whose watermark only moves forward —
would otherwise move past the older one and never send it. So an item that
can't be had right now stops its feed for this run, and the next run starts
with that same item; the feeds after it carry on.

Three answers are permanent, and those are stepped over for good rather than
retried: a video too short to be worth having (a YouTube Short), one too big
for the feed's size budget, and one its source has taken down. See
:data:`fetchit.media.PERMANENT`.

What a feed is done with is recorded in :mod:`fetchit.state`, after every item,
so a run that is killed partway has lost at most the item it was on.
"""

from __future__ import annotations

import fcntl
import os
import socket
import sys
import tempfile
import threading
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from . import cache, timing
from .config import Config, ConfigError
from .download import download_to, suffix_for
from .errorlog import log_exception, log_line
from .feeds import (
    FeedConfig,
    FeedError,
    FeedItem,
    ParsedFeed,
    SourceBlocked,
    apply_filters,
    feed_image,
    parse_feed,
    remember_validators,
)
from .library import (
    FEED_IMAGES,
    IMAGE_EXTS,
    STATE_DIR,
    feed_folders,
    place,
    prune,
    staging_dir,
    update_artwork,
    write_feed,
    write_item,
)
from .media import PERMANENT, TEXT, VIDEO_TIMEOUT, item_id, kind_of, resolve
from .state import State

# How long any one network read may sit there before it is given up on. This is
# feedparser's only timeout — it accepts none of its own, so it goes through the
# default socket timeout or it doesn't happen at all.
FEED_SOCKET_TIMEOUT = 30

# How many feeds are read at once. The work is nearly all waiting on other
# people's servers, so this is well above the core count on purpose; the ceiling
# is politeness to those servers rather than anything local.
DEFAULT_FEED_WORKERS = 8
FEED_WORKERS_ENV = "FETCHIT_FEED_WORKERS"

LOCK_NAME = "run.lock"

# How often a feed's artwork is compared with its source's. Artwork changes a
# few times a year at most, and for YouTube asking costs a yt-dlp launch.
ARTWORK_EVERY = timedelta(days=7)


class AlreadyRunning(Exception):
    """Raised when another fetchit run holds the library's lock."""


@dataclass
class Summary:
    """What a run did, for its last line."""

    fetched: int = 0
    skipped: int = 0
    failed: int = 0
    blocked: int = 0
    pruned: int = 0


@dataclass
class _Listing:
    """One feed's items as read, or the exception that stopped it.

    The parallel phase can't report a failure itself — a worker thread has no
    turn of its own, and a traceback surfacing out of order would belong to no
    feed in particular. So it carries the exception back instead, and the
    sequential phase raises it where that feed's turn is.
    """

    parsed: ParsedFeed | None = None
    items: list[FeedItem] = field(default_factory=list)
    error: Exception | None = None


@contextmanager
def single_run(root: str) -> Iterator[None]:
    """Hold the library's run lock for the block, or raise :class:`AlreadyRunning`.

    Two runs at once would both decide the same items are new and download them
    twice, and race each other writing the state. The lock is an ``flock``,
    which the kernel releases when the process ends however it ends.
    """
    folder = os.path.join(root, STATE_DIR)
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, LOCK_NAME)
    handle = open(path, "a")
    try:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise AlreadyRunning(
                f"another fetchit run is still going (lock held on {path})"
            ) from exc
        yield
    finally:
        handle.close()


def run(config: Config, *, dry_run: bool = False, only: list[str] | None = None) -> Summary:
    """Read every feed in ``config`` (or the ones ``only`` names) and fetch what's new.

    With ``dry_run`` nothing is downloaded, written or recorded: each item that
    would be is described instead, with the quality and size it would arrive at
    and where it would be filed.

    A feed that fails does not stop the others: it is reported on stderr and in
    the log, and the next run picks it up from the same item. Only one run
    happens at a time per library; a second raises :class:`AlreadyRunning`. A
    dry run takes no lock.
    """
    feeds = _select(config.feeds, only)
    with ExitStack() as stack:
        if not dry_run:
            stack.enter_context(single_run(config.library))
        return _locked_run(config, feeds, dry_run=dry_run)


def _select(feeds: list[FeedConfig], only: list[str] | None) -> list[FeedConfig]:
    if not only:
        return list(feeds)
    by_name = {cfg.name.casefold(): cfg for cfg in feeds}
    picked = []
    for name in only:
        cfg = by_name.get(name.strip().casefold())
        if cfg is None:
            raise ConfigError(
                f"No feed called {name!r} in the config. Feeds: "
                + ", ".join(repr(c.name) for c in feeds)
            )
        picked.append(cfg)
    return picked


def _locked_run(config: Config, feeds: list[FeedConfig], *, dry_run: bool) -> Summary:
    socket.setdefaulttimeout(FEED_SOCKET_TIMEOUT)
    started = time.monotonic()
    kind = " (dry run)" if dry_run else ""
    log_line(f"run started{kind}: {len(feeds)} feed(s)")
    summary = Summary()
    state = State.load(config.library)
    logged: str | None = None

    try:
        listings = _list_feeds(feeds)
        for cfg, listing in zip(feeds, listings):
            try:
                if listing.error is not None:
                    raise listing.error
                fetched, skipped = fetch_feed(config, cfg, listing, state, dry_run=dry_run)
                summary.fetched += fetched
                summary.skipped += skipped
            except SourceBlocked as exc:
                # Nothing went wrong: the source is deliberately unreachable
                # right now, and the feed picks up where it was next run.
                summary.blocked += 1
                print(f"[{cfg.name}] skipped: {exc}", file=sys.stderr)
                log_line(f"[{cfg.name}] skipped: {exc}")
            except Exception as exc:
                summary.failed += 1
                print(f"[{cfg.name}] stopped: {exc}", file=sys.stderr)
                logged = log_exception(f"feed {cfg.name!r}", exc) or logged

        if not dry_run:
            # After downloading, not before: expiry is nobody's hurry.
            summary.pruned = prune(
                config.library,
                {_folder_key(c.name): config.keep_days_for(c) for c in config.feeds},
                default=config.keep_days,
            )
    except BaseException as exc:
        log_line(f"run aborted after {_duration(started)}: {exc!r}")
        raise
    finally:
        cache.save()

    if summary.failed:
        where = f"; full tracebacks in {logged}" if logged else ""
        print(f"{summary.failed} of {len(feeds)} feed(s) failed{where}.", file=sys.stderr)
    if summary.blocked:
        print(
            f"{summary.blocked} of {len(feeds)} feed(s) skipped: source blocked from "
            "this machine.",
            file=sys.stderr,
        )
    pruned = f", {summary.pruned} expired" if summary.pruned else ""
    log_line(
        f"run finished in {_duration(started)}: {summary.fetched} item(s) "
        f"{'would be fetched' if dry_run else 'fetched'}{kind}{pruned}"
    )
    return summary


def _folder_key(name: str) -> str:
    from .library import FEED_MAX, safe_name

    return safe_name(name, limit=FEED_MAX)


def _list_feeds(feeds: list[FeedConfig]) -> list[_Listing]:
    """Read and filter every feed at once, in ``feeds`` order."""
    workers = _worker_count(len(feeds))
    with _whole_lines_stderr():
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="fetchit-feed") as pool:
            futures = [pool.submit(_list_feed, cfg) for cfg in feeds]
            return [future.result() for future in futures]


def _list_feed(cfg: FeedConfig) -> _Listing:
    """Read and filter one feed, catching whatever went wrong. Runs on a worker."""
    try:
        with timing.step("feed read", cfg.name):
            parsed = parse_feed(cfg)
            items = apply_filters(parsed.items, cfg.filters)
        return _Listing(parsed=parsed, items=items)
    except Exception as exc:
        return _Listing(error=exc)


def fetch_feed(
    config: Config, cfg: FeedConfig, listing: _Listing, state: State, *, dry_run: bool
) -> tuple[int, int]:
    """Download one feed's new items, oldest first. Returns ``(fetched, skipped)``.

    New means not yet done with (see :mod:`fetchit.state`) and published no
    earlier than the feed's starting point: ``since`` for a backfill, and
    otherwise the newest item there was when the feed was first seen — so
    following a channel starts with its latest video, not its last fifteen. At
    most ``max_per_run`` items are taken; the rest wait for the next run, which
    takes them first.

    Raises if an item can't be had right now, which leaves it and everything
    after it for the next run.
    """
    assert listing.parsed is not None
    parsed = listing.parsed
    root = config.library
    name = cfg.name

    listed = sorted((i for i in listing.items if i.published is not None), key=_when)

    after = cfg.since or state.after(name)
    if after is None and listed:
        # Seen for the first time: start from the newest item there is.
        after = _when(listed[-1])
        older = sum(1 for i in parsed.items if i.published is not None and i.published < after)
        print(f"[{name}] first run: starting from the newest item, passing over {older} older one(s).")
        if not dry_run:
            state.set_after(name, after)
            state.save()

    fresh = [
        i for i in listed
        if after is not None and _when(i) >= after and not state.seen(name, item_id(i))
    ]
    limit = cfg.max_per_run
    if cfg.max_per_day is not None:
        today = state.fetched_since(name, datetime.now(timezone.utc) - timedelta(days=1))
        limit = min(limit, max(0, cfg.max_per_day - today))
    due = fresh[:limit]
    if len(fresh) > len(due):
        why = "the next run" if limit == cfg.max_per_run else "max_per_day"
        print(f"[{name}] {len(fresh) - len(due)} more item(s) wait ({why}).")

    kinds = {kind for kind, _ in feed_folders(root, name)} | {kind_of(i) for i in due}
    if not dry_run and kinds:
        write_feed(root, cfg, parsed, kinds)

    fetched = skipped = 0
    for item in due:
        kind = kind_of(item)
        iid = item_id(item)
        if dry_run:
            if _describe(config, cfg, item, kind, iid):
                fetched += 1
            continue
        try:
            record = _fetch_item(root, cfg, parsed, item, kind, iid)
        except PERMANENT as exc:
            skipped += 1
            print(f"[{name}] skipped for good: {exc}", file=sys.stderr)
            log_line(f"[{name}] skipped for good: {exc}")
            state.mark(name, iid, f"skipped: {exc}")
            state.save()
            continue
        state.mark(name, iid)
        state.save()
        fetched += 1
        print(f"[{name}] {item.title} -> {os.path.relpath(record, root)}")

    if not dry_run:
        # Only now: every new item has been filed or stepped over, so "nothing
        # new since this answer" is true. A feed that stopped on a failure
        # raised before reaching here, and asks again in full next run.
        remember_validators(parsed)
        _refresh_artwork(root, cfg, parsed, state)
    return fetched, skipped


def _when(item: FeedItem) -> datetime:
    assert item.published is not None
    return item.published


def _fetch_item(
    root: str, cfg: FeedConfig, parsed: ParsedFeed, item: FeedItem, kind: str, iid: str
) -> str:
    """Download ``item``'s media and picture, and file it. Returns its record's path."""
    with tempfile.TemporaryDirectory(dir=staging_dir(root), prefix="download-") as into:
        media = None
        if kind != TEXT:
            plan = resolve(item, cfg.quality)
            media = plan.fetch(into, timeout=VIDEO_TIMEOUT)
        image = _download_image(item.image_url, into, cfg.name)
        return write_item(root, cfg, parsed, item, kind, item_id=iid, media=media, image=image)


def _download_image(url: str | None, into: str, label: str) -> str | None:
    """Download a picture into ``into``, or ``None`` if there is none to be had.

    A picture is decoration: an episode without one is still an episode, so a
    failed download costs the picture and nothing else.
    """
    if not url:
        return None
    suffix = suffix_for(url, ".jpg").lower()
    if suffix not in IMAGE_EXTS:
        suffix = ".jpg"
    fd, path = tempfile.mkstemp(dir=into, prefix="image-", suffix=suffix)
    os.close(fd)
    try:
        download_to(url, path)
    except FeedError as exc:
        print(f"[{label}] picture skipped: {exc}", file=sys.stderr)
        return None
    return path


def _describe(config: Config, cfg: FeedConfig, item: FeedItem, kind: str, iid: str) -> bool:
    """Say what a real run would do with ``item``. Returns whether it would file it."""
    when = item.published.strftime("%Y-%m-%d") if item.published else "undated"
    print(f"[{cfg.name}] {when}  {item.title}")
    if kind != TEXT:
        try:
            print(f"    {kind}: {resolve(item, cfg.quality).describe()}")
        except PERMANENT as exc:
            print(f"    nothing downloaded: {exc}")
            return False
        except FeedError as exc:
            print(f"    unavailable right now ({exc}) — would stop this feed and retry")
            return False
    else:
        print("    text: nothing to download, filed as a record")
    placement = place(config.library, cfg.name, item, kind, item_id=iid)
    rel = os.path.relpath(placement.path(".*" if kind != TEXT else ".json"), config.library)
    print(f"    -> {rel}")
    return True


def _refresh_artwork(root: str, cfg: FeedConfig, parsed: ParsedFeed, state: State) -> None:
    """Keep the feed's poster or cover in step with its source, about weekly.

    Never fails the feed: artwork that can't be had this time is a line on
    stderr, and is tried again next run.
    """
    folders = feed_folders(root, cfg.name)
    if not folders:
        return
    missing = any(
        not os.path.exists(os.path.join(folder, image))
        for kind, folder in folders
        for image in FEED_IMAGES[kind]
    )
    checked = state.artwork_checked(cfg.name)
    if not missing and checked is not None and datetime.now(timezone.utc) - checked < ARTWORK_EVERY:
        return

    url = parsed.image_url
    if not url:
        try:
            url = feed_image(cfg)
        except FeedError as exc:
            print(f"[{cfg.name}] artwork skipped: {exc}", file=sys.stderr)
            return
    if not url:
        state.set_artwork(cfg.name, state.artwork(cfg.name))
        state.save()
        return

    with tempfile.TemporaryDirectory(dir=staging_dir(root), prefix="artwork-") as into:
        image = _download_image(url, into, cfg.name)
        if image is None:
            return
        written = update_artwork(
            root, cfg, {kind for kind, _ in folders}, image, state.artwork(cfg.name)
        )
    state.set_artwork(cfg.name, written)
    state.save()


def _worker_count(feeds: int) -> int:
    """How many feeds to read at once — never more than there are feeds."""
    try:
        configured = int(os.environ.get(FEED_WORKERS_ENV, "") or DEFAULT_FEED_WORKERS)
    except ValueError:
        configured = DEFAULT_FEED_WORKERS
    return max(1, min(configured, feeds or 1))


def _duration(started: float) -> str:
    """``38s``, ``2m38s``, ``3h37m`` — the two largest units that say anything."""
    total = int(time.monotonic() - started)
    hours, rest = divmod(max(total, 0), 3600)
    minutes, seconds = divmod(rest, 60)
    if hours:
        return f"{hours}h{minutes:02d}m"
    return f"{minutes}m{seconds:02d}s" if minutes else f"{seconds}s"


@contextmanager
def _whole_lines_stderr() -> Iterator[None]:
    """Keep threads from writing over each other's warnings, for this block.

    Several feeds read at once means several of them can have something to
    complain about at once, and ``print`` reaches stderr as more than one write
    — the text, then the newline — so two threads interleave *within* a line.
    Buffering each thread's output until it has a full line, and writing that
    line under a lock, is enough to stop it.
    """
    real = sys.stderr
    sys.stderr = _LineSafeStderr(real)  # type: ignore[assignment]
    try:
        yield
    finally:
        sys.stderr.flush()
        sys.stderr = real


class _LineSafeStderr:
    """Buffers per thread, emits whole lines under one lock. See above."""

    def __init__(self, target) -> None:
        self._target = target
        self._lock = threading.Lock()
        self._local = threading.local()

    def write(self, text: str) -> int:
        buffered = getattr(self._local, "buffer", "") + text
        head, newline, tail = buffered.rpartition("\n")
        if newline:
            with self._lock:
                self._target.write(head + newline)
                self._target.flush()
        self._local.buffer = tail
        return len(text)

    def flush(self) -> None:
        leftover = getattr(self._local, "buffer", "")
        with self._lock:
            if leftover:
                self._target.write(leftover)
            self._target.flush()
        self._local.buffer = ""

    def __getattr__(self, name: str):
        return getattr(self._target, name)
