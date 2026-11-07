"""Remembered answers to questions whose answers don't change.

Strictly a speed cache, and the distinction matters: nothing here is ever
consulted to decide *what to download*. What a feed already has is recorded in
the library's own state file (see :mod:`fetchit.state`), beside the files it
describes. Deleting this file costs one slow run and changes nothing about the
outcome of it. That property is worth protecting: if something ever needs to be
cached that *would* change what gets downloaded, it does not belong in here.

What it holds is the work a run repeats for no reason. An ARTE programme's
rights window began when it began; a YouTube ``@handle`` has named the same
channel since it was created; a video went up when it went up; a feed that has
not changed will say so if it is asked with the ETag it gave out last time.
Re-deriving those costs a run some forty HTTP round trips and a couple of yt-dlp
launches, every run, to arrive back at yesterday's answer.

The YouTube entries a backfill leans on are a closer call than the rest, because
that feed's items are built out of them — but they stay on the right side of the
line, and it is worth being able to say why. Neither is a record of what fetchit
*did*: one is the order YouTube lists a channel's uploads in, the other is when
each of them went up, and both come back identical when they are asked for
again. Deleting them costs a slow run and one long listing, and the same episode
is downloaded either way.

Entries are namespaced and carry the time they were stored, so each kind of
answer can be trusted for as long as it deserves — see the ``*_TTL`` constants.
The whole file is read once at the start of a run and written once at the end,
because a run is short and the alternative is a hundred small writes; it is held
under a lock because feeds are read in parallel (:func:`fetchit.run._list_feeds`).

Corruption is not an error condition. An unreadable, truncated or half-written
cache is treated as an empty one and simply refilled — the only cost is the slow
run it was there to avoid.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from typing import Any

# Where the cache lives when nothing says otherwise. Under the user's cache
# directory rather than beside feeds.json, because that is the directory whose
# whole contract is "safe to delete, will be rebuilt".
CACHE_ENV = "FETCHIT_CACHE"

# An ARTE programme's rights window began on a fixed date; it is not going to
# begin on a different one. Kept indefinitely.
ARTE_PUBLISHED_TTL = None

# A programme that gave up no date is asked again before long: the usual reason
# is that its rights window has not been published yet, which is a temporary
# state, and caching "no" for a month would keep a real episode out of the feed
# permanently.
ARTE_UNDATED_TTL = 6 * 60 * 60

# A @handle points at one channel for the life of the channel. Re-checked
# eventually only so that a handle changing hands is not permanent.
YOUTUBE_CHANNEL_TTL = 90 * 24 * 60 * 60

# Durations for a channel's latest uploads — the one thing here that genuinely
# goes stale, since the "latest fifteen" moves. Short enough that a new upload is
# never held back by more than one run's worth of time.
YOUTUBE_DURATIONS_TTL = 60 * 60

# A channel's uploads playlist, in order: what a YouTube backfill walks (a feed
# with ``since``). It grows at the newest end and never reorders
# behind that, so a week-old copy is a week-old copy of a correct answer, and
# re-reading it costs a single request that returns a thousand videos.
YOUTUBE_UPLOADS_TTL = 7 * 24 * 60 * 60

# When a particular video went up. Fixed for the life of the video, and dear to
# ask for — one yt-dlp launch each, because the playlist listing doesn't carry
# dates. Kept indefinitely, like ARTE's.
YOUTUBE_PUBLISHED_TTL = None

# A video fetchit has been told it cannot have: age-gated, private,
# members-only, removed. Unlike ARTE's undated programmes this is kept for good,
# and the asymmetry is deliberate. A backfill walks past such a video; if it were
# asked about again later and answered, it would land in the library behind
# episodes its readers had already moved past, and never be sent on — a silently
# missing episode, which is the one outcome worth ruling out. A video we cannot read the date of is one
# we could not have downloaded either, so nothing is lost by it being final.
YOUTUBE_UNDATED_TTL = None

_lock = threading.Lock()
_data: dict[str, dict[str, Any]] | None = None
_dirty = False


def path() -> str:
    """Where the cache file is, honouring :data:`CACHE_ENV` then XDG."""
    override = os.environ.get(CACHE_ENV, "").strip()
    if override:
        return os.path.expanduser(override)
    base = os.environ.get("XDG_CACHE_HOME", "").strip() or os.path.join(
        os.path.expanduser("~"), ".cache"
    )
    return os.path.join(base, "fetchit", "cache.json")


def _loaded() -> dict[str, dict[str, Any]]:
    """The cache contents, reading the file the first time. Call under ``_lock``."""
    global _data
    if _data is None:
        try:
            with open(path(), encoding="utf-8") as fh:
                raw = json.load(fh)
            _data = raw if isinstance(raw, dict) else {}
        except (OSError, ValueError):
            # Missing, unreadable, or garbage: all mean "nothing remembered".
            _data = {}
    return _data


def get(namespace: str, key: str, *, max_age: float | None = None) -> Any | None:
    """Return the remembered value, or ``None`` if there isn't a usable one.

    ``max_age`` is in seconds; ``None`` means an answer of this kind never goes
    stale. An entry stored in the future — a clock that moved — is treated as
    fresh rather than discarded, since the alternative helps nobody.
    """
    with _lock:
        entry = _loaded().get(namespace, {}).get(key)
        if not isinstance(entry, dict) or "value" not in entry:
            return None
        if max_age is not None:
            stored_at = entry.get("stored_at")
            if not isinstance(stored_at, (int, float)):
                return None
            if time.time() - stored_at > max_age:
                return None
        return entry["value"]


def put(namespace: str, key: str, value: Any) -> None:
    """Remember ``value`` under ``namespace``/``key`` for later runs."""
    global _dirty
    with _lock:
        _loaded().setdefault(namespace, {})[key] = {
            "value": value,
            "stored_at": time.time(),
        }
        _dirty = True


def save() -> None:
    """Write the cache out, if anything changed. Failing to is not an error.

    Written to a temporary file in the same directory and renamed over the real
    one, so a run interrupted mid-write leaves the previous cache intact rather
    than a half-written file for the next run to trip over.
    """
    global _dirty
    with _lock:
        if not _dirty or _data is None:
            return
        target = path()
        try:
            os.makedirs(os.path.dirname(target), exist_ok=True)
            fd, tmp = tempfile.mkstemp(
                dir=os.path.dirname(target), prefix=".cache-", suffix=".json"
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(_data, fh)
                os.replace(tmp, target)
            except BaseException:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
        except (OSError, TypeError, ValueError):
            # A cache that can't be written is a cache that isn't there, which
            # is a slower next run and nothing worse. Not worth a word.
            return
        _dirty = False


def clear() -> None:
    """Forget everything, in memory. For tests, and for a caller that must."""
    global _data, _dirty
    with _lock:
        _data = {}
        _dirty = True
