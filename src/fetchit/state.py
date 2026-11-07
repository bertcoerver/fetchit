"""What fetchit has already done for each feed: the one record that decides what
gets downloaded.

Each feed keeps two things. The ids of the items it is done with — downloaded,
or stepped over for good (a Short, a video whose rights have gone) — so nothing
is fetched twice, including after the library has pruned it. And the moment it
starts from: a feed seen for the first time takes its newest item and nothing
published before it, so that following a channel starts with its latest video
rather than its last fifteen, or a podcast's newest episode rather than its
four thousand. One date says that, where marking every older item would be a
list as long as the feed; see :func:`fetchit.run.fetch_feed`.

It lives in the library (``.fetchit/state.json``) rather than beside the cache,
because it describes the library: a library copied to another disk takes it
along and carries on, and a library deleted to start over takes it along too.
Unlike the cache, losing it is not free — every feed would start over at its
newest item, and a backfill would start from its ``since`` again, skipping
nothing but downloading again what was pruned.

Written after every item rather than at the end of a run, through a temporary
file renamed over the real one, so a run that is killed partway has recorded
exactly the items it filed.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import datetime, timezone

from .library import STATE_DIR

STATE_FILE = "state.json"


class State:
    """The state of one library. Load with :meth:`load`; :meth:`save` after changes."""

    def __init__(self, path: str, data: dict | None = None) -> None:
        self.path = path
        self._data = data if isinstance(data, dict) else {}
        self._data.setdefault("format", 1)
        if not isinstance(self._data.get("feeds"), dict):
            self._data["feeds"] = {}

    @classmethod
    def load(cls, root: str) -> "State":
        """Read the state of the library at ``root``; a missing file is an empty one.

        An unreadable one is not quietly emptied: that would start every feed
        over, which is a decision rather than a recovery. It is moved aside
        with a warning, so the damage is visible and the old file is still there
        to look at.
        """
        path = os.path.join(root, STATE_DIR, STATE_FILE)
        try:
            with open(path, encoding="utf-8") as fh:
                return cls(path, json.load(fh))
        except FileNotFoundError:
            return cls(path)
        except (OSError, ValueError) as exc:
            aside = f"{path}.broken-{datetime.now():%Y%m%d%H%M%S}"
            try:
                os.replace(path, aside)
            except OSError:
                pass
            print(
                f"fetchit's state file {path} could not be read ({exc}); moved it "
                f"to {aside} and starting from an empty one.",
                file=sys.stderr,
            )
            return cls(path)

    def _feed(self, name: str) -> dict:
        feeds = self._data["feeds"]
        entry = feeds.get(name)
        if not isinstance(entry, dict):
            entry = feeds[name] = {}
        if not isinstance(entry.get("seen"), dict):
            entry["seen"] = {}
        return entry

    def known(self, name: str) -> bool:
        """Whether feed ``name`` has a starting point yet."""
        return self.after(name) is not None

    def after(self, name: str) -> datetime | None:
        """What feed ``name`` starts from: nothing published before this is wanted."""
        entry = self._data["feeds"].get(name)
        value = entry.get("after") if isinstance(entry, dict) else None
        try:
            return datetime.fromisoformat(value) if isinstance(value, str) else None
        except ValueError:
            return None

    def set_after(self, name: str, when: datetime) -> None:
        """Start feed ``name`` from ``when``."""
        self._feed(name)["after"] = when.isoformat()

    def seen(self, name: str, item_id: str) -> bool:
        """Whether feed ``name`` is done with the item ``item_id``."""
        entry = self._data["feeds"].get(name)
        return isinstance(entry, dict) and item_id in (entry.get("seen") or {})

    def mark(self, name: str, item_id: str, why: str = "fetched") -> None:
        """Record that feed ``name`` is done with ``item_id``, and ``why``."""
        stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self._feed(name)["seen"][item_id] = f"{stamp} {why}"

    def fetched_since(self, name: str, when: datetime) -> int:
        """How many items feed ``name`` has downloaded since ``when``."""
        entry = self._data["feeds"].get(name)
        seen = entry.get("seen") if isinstance(entry, dict) else None
        count = 0
        for note in (seen or {}).values():
            stamp, _, why = str(note).partition(" ")
            if why != "fetched":
                continue
            try:
                if datetime.fromisoformat(stamp) >= when:
                    count += 1
            except ValueError:
                continue
        return count

    def artwork(self, name: str) -> dict[str, str]:
        """What fetchit last wrote as feed ``name``'s artwork: file -> hash."""
        value = self._feed(name).get("artwork")
        return dict(value) if isinstance(value, dict) else {}

    def set_artwork(self, name: str, written: dict[str, str]) -> None:
        entry = self._feed(name)
        entry["artwork"] = written
        entry["artwork_checked"] = datetime.now(timezone.utc).isoformat(timespec="seconds")

    def artwork_checked(self, name: str) -> datetime | None:
        """When feed ``name``'s artwork was last compared with its source's."""
        value = self._data["feeds"].get(name, {}).get("artwork_checked")
        try:
            return datetime.fromisoformat(value) if isinstance(value, str) else None
        except ValueError:
            return None

    def save(self) -> None:
        """Write the state out, atomically. A failure here is raised: it matters."""
        folder = os.path.dirname(self.path)
        os.makedirs(folder, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=folder, prefix=".state-", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(self._data, fh, ensure_ascii=False, indent=1, sort_keys=True)
                fh.write("\n")
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
